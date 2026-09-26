#!/usr/bin/env python3
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HIST_URL = "https://www.nlcbplaywhelotto.com/nlcb-play-whe-results/"
MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
TIME_MAP = {
    "Morning": ("10:30 AM", 630),
    "Midday": ("1:00 PM", 780),
    "Afternoon": ("4:00 PM", 960),
    "Evening": ("7:00 PM", 1140),
}
KNOWN_SOURCE_GAP_MONTHS = {
    (1995, 2),
    (2001, 11),
    (2003, 6),
    (2020, 4),
    (2020, 5),
    (2021, 6),
    (2021, 7),
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 PlayWheData-YearBackfill/2.0",
    "Accept": "text/html,application/xhtml+xml",
}

def request(session, method, url, **kwargs):
    last = None
    for attempt in range(1, 5):
        try:
            r = session.request(method, url, headers=HEADERS, timeout=25, **kwargs)
            r.raise_for_status()
            return r
        except Exception as exc:
            last = exc
            if attempt < 4:
                wait = 4 * attempt
                print(f"retry attempt={attempt} wait={wait}s error={exc}", flush=True)
                time.sleep(wait)
    raise RuntimeError(f"request failed after retries: {last}")

def get_sid(session):
    r = request(session, "GET", HIST_URL)
    soup = BeautifulSoup(r.text, "html.parser")
    node = soup.find("input", {"name": "sid"})
    if node is None or not node.get("value"):
        raise RuntimeError("historical site did not provide a search sid token")
    return node.get("value")

def parse_month(session, sid, month_name, year):
    r = request(session, "POST", HIST_URL, data={
        "playwhe_month": month_name,
        "playwhe_year": str(year),
        "dateBtn": "SEARCH",
        "sid": sid,
    })
    soup = BeautifulSoup(r.text, "html.parser")
    new_sid = soup.find("input", {"name": "sid"})
    if new_sid is not None and new_sid.get("value"):
        sid = new_sid.get("value")

    table = soup.find("table", {"id": "monthResults"})
    if table is None:
        raise RuntimeError(f"Month table missing for {month_name}-{year}")

    rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 4:
            continue
        try:
            draw_number = int(cells[0])
            date = datetime.strptime(cells[1], "%d-%b-%y").date().isoformat()
            raw_time = cells[2].strip()
            winning = int(cells[3])
        except Exception:
            continue
        if raw_time not in TIME_MAP:
            raise RuntimeError(f"draw #{draw_number}: unknown time label {raw_time!r}")
        if winning not in range(1, 37):
            raise RuntimeError(f"draw #{draw_number}: invalid winning number {winning}")
        draw_time, draw_minutes = TIME_MAP[raw_time]
        rows.append({
            "draw_number": draw_number,
            "date": date,
            "draw_time": draw_time,
            "draw_minutes": draw_minutes,
            "winning_number": winning,
            "source_type": "nlcbplaywhelotto_archive",
            "source_url": HIST_URL,
            "source_lookup": f"{month_name}-{year}",
            "source_verification": "month_archive_no_badge",
            "official_cross_checked": False,
        })
    return rows, sid

def main():
    year = int(os.environ["YEAR"])
    current_year = datetime.utcnow().year
    current_month = datetime.utcnow().month
    first_month = 7 if year == 1994 else 1
    last_month = current_month if year == current_year else 12

    session = requests.Session()
    sid = get_sid(session)
    rows = []
    missing_months = []

    for month_num in range(first_month, last_month + 1):
        month_name = MONTHS[month_num - 1]
        try:
            month_rows, sid = parse_month(session, sid, month_name, year)
            rows.extend(month_rows)
            print(f"year={year} month={month_name} rows={len(month_rows)} total={len(rows)}", flush=True)
        except Exception as exc:
            msg = str(exc)
            if (year, month_num) in KNOWN_SOURCE_GAP_MONTHS and msg.startswith("Month table missing"):
                missing_months.append({
                    "year": year,
                    "month": month_num,
                    "label": f"{month_name} {year}",
                    "reason": "No month table available from historical source",
                })
                print(f"known_source_gap {month_name}-{year}", flush=True)
            else:
                raise
        time.sleep(1.5)

    # Validate uniqueness inside the year.
    by_number = {}
    slots = {}
    for row in rows:
        n = row["draw_number"]
        if n in by_number and by_number[n] != row:
            raise RuntimeError(f"conflicting duplicate draw #{n}")
        by_number[n] = row
        slot = (row["date"], row["draw_minutes"])
        if slot in slots and slots[slot] != n:
            raise RuntimeError(f"duplicate date/time slot {slot}")
        slots[slot] = n

    out = {
        "year": year,
        "rows": sorted(by_number.values(), key=lambda r: r["draw_number"]),
        "missing_months": missing_months,
    }
    Path("year-output").mkdir(exist_ok=True)
    path = Path("year-output") / f"year-{year}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"year_complete year={year} rows={len(out['rows'])} missing_months={len(missing_months)}", flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
