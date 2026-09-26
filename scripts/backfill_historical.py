#!/usr/bin/env python3
"""
Backfill Play Whe history from nlcbplaywhelotto.com's month-search archive.

This source is NOT the official NLCB website. It is used only for historical
coverage that the official NLCB REST API no longer exposes. Overlapping rows
are compared against the official GitHub master and official NLCB data always
takes precedence.
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HIST_URL = "https://www.nlcbplaywhelotto.com/nlcb-play-whe-results/"
DATA_DIR = Path("data")
OFFICIAL_OR_CURRENT_MASTER = DATA_DIR / "history.json"
HISTORICAL_PATH = DATA_DIR / "historical_history.json"
HISTORICAL_MANIFEST_PATH = DATA_DIR / "historical_manifest.json"
CONFLICTS_PATH = DATA_DIR / "historical_conflicts.json"

START_YEAR = 1994
START_MONTH = 7
KNOWN_SOURCE_GAP_MONTHS = {
    (1995, 2),
    (2001, 11),
    (2003, 6),
    (2020, 4),
    (2020, 5),
    (2021, 6),
    (2021, 7),
}
MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
TIME_MAP = {
    "Morning": ("10:30 AM", 630),
    "Midday": ("1:00 PM", 780),
    "Afternoon": ("4:00 PM", 960),
    "Evening": ("7:00 PM", 1140),
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/140 Safari/537.36 PlayWheData-HistoricalBackfill/1.0",
    "Accept": "text/html,application/xhtml+xml",
}

def request(session, method, url, **kwargs):
    last = None
    for attempt in range(1, 6):
        try:
            r = session.request(method, url, headers=HEADERS, timeout=30, **kwargs)
            r.raise_for_status()
            return r
        except Exception as exc:
            last = exc
            if attempt < 5:
                wait = min(3 * attempt, 12)
                print(f"request_retry attempt={attempt} wait={wait}s error={exc}", flush=True)
                time.sleep(wait)
    raise RuntimeError(f"Request failed after retries: {url}: {last}")

def get_sid(session):
    r = request(session, "GET", HIST_URL)
    soup = BeautifulSoup(r.text, "html.parser")
    node = soup.find("input", {"name": "sid"})
    if node is None or not node.get("value"):
        raise RuntimeError("Historical site did not provide its search sid token.")
    return node.get("value")

def parse_month(session, sid, month_name, year):
    payload = {
        "playwhe_month": month_name,
        "playwhe_year": str(year),
        "dateBtn": "SEARCH",
        "sid": sid,
    }
    r = request(session, "POST", HIST_URL, data=payload)
    soup = BeautifulSoup(r.text, "html.parser")

    new_sid = soup.find("input", {"name": "sid"})
    if new_sid is not None and new_sid.get("value"):
        sid = new_sid.get("value")

    table = soup.find("table", {"id": "monthResults"})
    if table is None:
        # Months before the game began, or a genuinely empty month, can have no table.
        body = " ".join(soup.stripped_strings)
        if f"Showing Results for: {month_name}-{str(year)[-2:]}" in body:
            return [], sid
        raise RuntimeError(f"Month table missing for {month_name}-{year}.")

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
            raise RuntimeError(
                f"Historical draw #{draw_number} has unknown time label {raw_time!r}."
            )
        if winning not in range(1, 37):
            raise RuntimeError(
                f"Historical draw #{draw_number} has invalid winning number {winning}."
            )

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

def month_range(end_year, end_month):
    for year in range(START_YEAR, end_year + 1):
        first = START_MONTH if year == START_YEAR else 1
        last = end_month if year == end_year else 12
        for month in range(first, last + 1):
            yield year, month

def load_official():
    if not OFFICIAL_OR_CURRENT_MASTER.exists():
        return []
    try:
        rows = json.loads(OFFICIAL_OR_CURRENT_MASTER.read_text(encoding="utf-8"))
    except Exception:
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # At the moment the central history is the official NLCB REST dataset.
        # Once combined history is introduced, retain only rows explicitly official.
        if row.get("source_type") and row.get("source_type") != "official_nlcb_rest":
            continue
        if not row.get("source_type") and not row.get("source_post_id"):
            continue
        result.append(row)
    return result

def core(row):
    return (
        int(row["draw_number"]),
        str(row["date"]),
        int(row["draw_minutes"]),
        int(row["winning_number"]),
    )

def validate_historical(rows):
    by_number = {}
    slot_to_number = {}
    for row in rows:
        n = row["draw_number"]
        old = by_number.get(n)
        if old is not None and core(old) != core(row):
            raise RuntimeError(
                f"Conflicting historical records for draw #{n}: {core(old)} vs {core(row)}"
            )
        by_number[n] = row

        slot = (row["date"], row["draw_minutes"])
        other = slot_to_number.get(slot)
        if other is not None and other != n:
            raise RuntimeError(
                f"Historical archive has two draw numbers for {slot}: #{other} and #{n}"
            )
        slot_to_number[slot] = n

    result = sorted(
        by_number.values(),
        key=lambda r: (r["draw_number"], r["date"], r["draw_minutes"]),
    )

    previous = None
    for row in result:
        if previous is not None and row["draw_number"] <= previous:
            raise RuntimeError("Historical draw numbers are not strictly increasing.")
        previous = row["draw_number"]

    numbers = set(by_number)
    gaps = []
    if numbers:
        for n in range(min(numbers), max(numbers) + 1):
            if n not in numbers:
                gaps.append(n)
    return result, gaps

def compare_official(historical, official):
    official_by_number = {int(r["draw_number"]): r for r in official}
    matches = 0
    conflicts = []
    for row in historical:
        official_row = official_by_number.get(row["draw_number"])
        if official_row is None:
            continue
        if core(row) == core(official_row):
            row["official_cross_checked"] = True
            matches += 1
        else:
            conflicts.append({
                "draw_number": row["draw_number"],
                "historical": {
                    "date": row["date"],
                    "draw_minutes": row["draw_minutes"],
                    "winning_number": row["winning_number"],
                },
                "official": {
                    "date": official_row["date"],
                    "draw_minutes": official_row["draw_minutes"],
                    "winning_number": official_row["winning_number"],
                },
            })
    return matches, conflicts

def main():
    now = datetime.now()
    end_year = int(os.environ.get("END_YEAR", now.year))
    end_month = int(os.environ.get("END_MONTH", now.month))

    session = requests.Session()
    sid = get_sid(session)
    all_rows = []
    failed_months = []

    months = list(month_range(end_year, end_month))
    previous_year = None
    for idx, (year, month_num) in enumerate(months, start=1):
        month_name = MONTHS[month_num - 1]

        # Refresh the session/token at each new year so the archive site does not
        # accumulate a very long-lived POST session during this one-time backfill.
        if previous_year is not None and year != previous_year:
            time.sleep(4)
            session.close()
            session = requests.Session()
            sid = get_sid(session)
        previous_year = year

        try:
            rows, sid = parse_month(session, sid, month_name, year)
        except Exception as first_exc:
            print(
                f"month_retry {month_name}-{year} first_error={first_exc}",
                flush=True,
            )
            time.sleep(20)
            session.close()
            session = requests.Session()
            try:
                sid = get_sid(session)
                rows, sid = parse_month(session, sid, month_name, year)
            except Exception as second_exc:
                message = str(second_exc)
                if (
                    (year, month_num) in KNOWN_SOURCE_GAP_MONTHS
                    and message.startswith("Month table missing for ")
                ):
                    print(
                        f"known_source_gap {month_name}-{year} error={second_exc}",
                        flush=True,
                    )
                    failed_months.append((year, month_num, message))
                    continue
                raise RuntimeError(
                    f"Unexpected historical-source failure for "
                    f"{month_name}-{year}: {second_exc}"
                ) from second_exc

        all_rows.extend(rows)
        print(
            f"month={month_name}-{year} rows={len(rows)} "
            f"total={len(all_rows)} progress={idx}/{len(months)}",
            flush=True,
        )
        time.sleep(1.2)

    historical, gaps = validate_historical(all_rows)

    missing_months = [
        {
            "year": y,
            "month": m,
            "label": f"{MONTHS[m-1]} {y}",
            "reason": "No month table available from historical source",
        }
        for y, m, _ in sorted(failed_months)
    ]

    # Group consecutive unavailable months so a missing draw-number search can be
    # associated with the surrounding archive gap without inventing any results.
    missing_spans = []
    failed_keys = sorted((y, m) for y, m, _ in failed_months)
    if failed_keys:
        groups = []
        current = [failed_keys[0]]
        for key in failed_keys[1:]:
            py, pm = current[-1]
            ny, nm = key
            expected = (py + 1, 1) if pm == 12 else (py, pm + 1)
            if key == expected:
                current.append(key)
            else:
                groups.append(current)
                current = [key]
        groups.append(current)

        def row_month(row):
            d = datetime.strptime(row["date"], "%Y-%m-%d")
            return (d.year, d.month)

        for group in groups:
            first = group[0]
            last = group[-1]
            before = [r["draw_number"] for r in historical if row_month(r) < first]
            after = [r["draw_number"] for r in historical if row_month(r) > last]
            start_draw = max(before) + 1 if before else None
            end_draw = min(after) - 1 if after else None
            if len(group) == 1:
                label = f"{MONTHS[first[1]-1]} {first[0]}"
            else:
                label = (
                    f"{MONTHS[first[1]-1]} {first[0]}–"
                    f"{MONTHS[last[1]-1]} {last[0]}"
                )
            missing_spans.append({
                "label": label,
                "start_year": first[0],
                "start_month": first[1],
                "end_year": last[0],
                "end_month": last[1],
                "possible_draw_start": start_draw,
                "possible_draw_end": end_draw,
                "reason": "Historical archive returned no month table",
            })
    official = load_official()
    matches, conflicts = compare_official(historical, official)

    # Official NLCB is authoritative in overlap. Conflicts are preserved for audit,
    # but do not corrupt the historical source file.
    overlap_count = sum(
        1 for r in historical
        if any(int(o["draw_number"]) == r["draw_number"] for o in official)
    )
    conflict_rate = (len(conflicts) / overlap_count) if overlap_count else 0.0

    # A broad mismatch would mean this source/parser is unsafe. A tiny number of
    # disagreements can be audited individually while official NLCB wins.
    if overlap_count >= 100 and conflict_rate > 0.01:
        raise RuntimeError(
            f"Historical source disagrees with official NLCB too often: "
            f"{len(conflicts)}/{overlap_count} overlap records."
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORICAL_PATH.write_text(
        json.dumps(historical, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    CONFLICTS_PATH.write_text(
        json.dumps(conflicts, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "schema_version": 2,
        "source": "nlcbplaywhelotto.com historical month archive",
        "source_url": HIST_URL,
        "source_is_official_nlcb": False,
        "earliest_draw_number": historical[0]["draw_number"] if historical else None,
        "earliest_date": historical[0]["date"] if historical else None,
        "latest_draw_number": historical[-1]["draw_number"] if historical else None,
        "latest_date": historical[-1]["date"] if historical else None,
        "total_draws": len(historical),
        "sequence_gap_count": len(gaps),
        "sequence_gaps": gaps,
        "missing_month_count": len(missing_months),
        "missing_months": missing_months,
        "missing_month_spans": missing_spans,
        "official_overlap_count": overlap_count,
        "official_exact_matches": matches,
        "official_conflict_count": len(conflicts),
        "official_conflict_rate": conflict_rate,
        "note": (
            "Historical rows are sourced from a third-party archive. "
            "Official NLCB records take precedence wherever they overlap. "
            "Months listed in missing_months are source-availability gaps and "
            "must not be interpreted as proof that no lottery draws occurred."
        ),
    }
    HISTORICAL_MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f"historical_backfill_complete total={len(historical)} "
        f"earliest=#{manifest['earliest_draw_number']} "
        f"latest=#{manifest['latest_draw_number']} "
        f"gaps={len(gaps)} overlap={overlap_count} "
        f"matches={matches} conflicts={len(conflicts)} "
        f"missing_months={len(missing_months)}"
    )

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
