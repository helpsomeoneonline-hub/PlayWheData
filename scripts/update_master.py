#!/usr/bin/env python3
# Central master-data builder for Play Whe Insight.
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.nlcbgames.com/play-whe/"
CHECK_RESULTS_URL = "https://www.nlcbgames.com/check-results/"
DATA_DIR = Path("data")
HISTORY_PATH = DATA_DIR / "history.json"
LATEST_PATH = DATA_DIR / "latest.json"
MANIFEST_PATH = DATA_DIR / "manifest.json"

ARCHIVE_DATE_RE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+(\d{1,2})\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})$",
    re.I,
)
DRAW_RE = re.compile(
    r"^(10[:.]30\s*AM|1(?::00)?\s*PM|4(?::00)?\s*PM|7(?::00)?\s*PM)"
    r"\s+Draw#\s*0*(\d+)\b",
    re.I,
)
NUMBER_RE = re.compile(r"^(\d{1,2})(?:\s+.*)?$")

TIME_TO_MINUTES = {
    "10:30 AM": 630,
    "1:00 PM": 780,
    "4:00 PM": 960,
    "7:00 PM": 1140,
}
TIME_ORDER = list(TIME_TO_MINUTES)

def normalize_time(raw: str) -> str:
    compact = raw.upper().replace(" ", "").replace(".", ":")
    return {
        "10:30AM": "10:30 AM",
        "1:00PM": "1:00 PM",
        "1PM": "1:00 PM",
        "4:00PM": "4:00 PM",
        "4PM": "4:00 PM",
        "7:00PM": "7:00 PM",
        "7PM": "7:00 PM",
    }.get(compact, raw.strip())

def get_lines(url: str):
    r = requests.get(
        url,
        headers={"User-Agent": "PlayWheData/1.0 (+https://github.com/helpsomeoneonline-hub/PlayWheData)"},
        timeout=30,
    )
    if r.status_code in (404, 410):
        return []
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    return [re.sub(r"\s+", " ", x).strip() for x in soup.stripped_strings if x.strip()]

def parse_page(lines, source_url: str):
    current_date = None
    results = []
    for i, line in enumerate(lines):
        if ARCHIVE_DATE_RE.match(line):
            current_date = datetime.strptime(line, "%a, %d %b %Y").date().isoformat()
            continue

        m = DRAW_RE.match(line)
        if not m or current_date is None:
            continue

        draw_time = normalize_time(m.group(1))
        draw_number = int(m.group(2))
        winning_number = find_winning(lines, i + 1)
        if winning_number is None:
            continue

        results.append({
            "draw_number": draw_number,
            "date": current_date,
            "draw_time": draw_time,
            "draw_minutes": TIME_TO_MINUTES[draw_time],
            "winning_number": winning_number,
            "source_url": BASE_URL,
        })
    return results

def find_winning(lines, start):
    saw_label = False
    for line in lines[start:start + 10]:
        if line.lower().startswith("jackpot"):
            break
        if line.lower() in ("winning number", "winning numbers"):
            saw_label = True
            continue
        if not saw_label:
            continue
        m = NUMBER_RE.match(line)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 36:
                return n
    return None

def scrape_archive(max_pages=250):
    by_number = {}
    empty_streak = 0
    page_counts = []

    for page in range(1, max_pages + 1):
        url = BASE_URL if page == 1 else f"{BASE_URL}page/{page}/"
        lines = get_lines(url)
        rows = parse_page(lines, url)
        page_counts.append(len(rows))

        if not rows:
            empty_streak += 1
            if empty_streak >= 3:
                break
        else:
            empty_streak = 0

        for row in rows:
            old = by_number.get(row["draw_number"])
            if old:
                old_core = {k: old[k] for k in ("draw_number", "date", "draw_time", "draw_minutes", "winning_number")}
                new_core = {k: row[k] for k in ("draw_number", "date", "draw_time", "draw_minutes", "winning_number")}
                if old_core != new_core:
                    raise RuntimeError(
                        f'Conflicting duplicate draw #{row["draw_number"]}: {old_core} vs {new_core}'
                    )
                continue
            by_number[row["draw_number"]] = row

        print(f"page={page} page_rows={len(rows)} total_unique={len(by_number)}")

    rows = sorted(
        by_number.values(),
        key=lambda d: (d["date"], TIME_ORDER.index(d["draw_time"]), d["draw_number"]),
    )
    return rows, page_counts

def validate(rows):
    issues = []
    if not rows:
        return ["No rows parsed"]

    seen_numbers = set()
    seen_slots = set()
    previous = None

    for row in rows:
        num = row["draw_number"]
        if num in seen_numbers:
            issues.append(f"Duplicate draw number #{num}")
        seen_numbers.add(num)

        slot = (row["date"], row["draw_time"])
        if slot in seen_slots:
            issues.append(f"Duplicate date/time slot {slot}")
        seen_slots.add(slot)

        if row["winning_number"] not in range(1, 37):
            issues.append(f"Invalid winning number for draw #{num}")

        if row["draw_time"] not in TIME_TO_MINUTES:
            issues.append(f"Invalid draw time for draw #{num}")

        if previous is not None and num <= previous:
            issues.append(f"Draw number order went backwards: {previous} then {num}")
        previous = num

    nums = sorted(seen_numbers)
    gaps = []
    for a, b in zip(nums, nums[1:]):
        if b > a + 1:
            gaps.append({"after": a, "before": b, "missing_count": b - a - 1})

    return issues, gaps

def verify_latest_against_second_source(latest):
    lines = get_lines(CHECK_RESULTS_URL)
    target = latest["draw_number"]

    for i, line in enumerate(lines):
        if not re.search(rf"\bDraw#\s*0*{target}\b", line, re.I):
            continue
        winning = find_winning(lines, i + 1)
        if winning is None:
            continue
        if winning != latest["winning_number"]:
            raise RuntimeError(
                f'Official-source mismatch for #{target}: '
                f'Play Whe={latest["winning_number"]}, Check Results={winning}'
            )
        return True

    raise RuntimeError(
        f'Draw #{target} not independently confirmed on NLCB Check Results page'
    )

def stable_json_bytes(obj):
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

def load_existing_history():
    if not HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []

def merge_rows(existing, fresh):
    by_number = {row["draw_number"]: row for row in existing}
    for row in fresh:
        old = by_number.get(row["draw_number"])
        if old:
            old_core = {k: old[k] for k in ("draw_number", "date", "draw_time", "draw_minutes", "winning_number")}
            new_core = {k: row[k] for k in ("draw_number", "date", "draw_time", "draw_minutes", "winning_number")}
            if old_core != new_core:
                raise RuntimeError(
                    f'Existing master conflict for draw #{row["draw_number"]}: {old_core} vs {new_core}'
                )
        if old:
            continue
        by_number[row["draw_number"]] = row
    return sorted(
        by_number.values(),
        key=lambda d: (d["date"], TIME_ORDER.index(d["draw_time"]), d["draw_number"]),
    )

def main():
    existing = load_existing_history()
    full_rebuild = os.environ.get("FULL_REBUILD", "").lower() == "true" or not existing

    if full_rebuild:
        print("mode=full_rebuild")
        rows, page_counts = scrape_archive(max_pages=250)
    else:
        print("mode=incremental")
        fresh, page_counts = scrape_archive(max_pages=4)
        rows = merge_rows(existing, fresh)

    issues, gaps = validate(rows)
    if issues:
        raise RuntimeError("Integrity validation failed: " + "; ".join(issues[:10]))

    latest = rows[-1]
    verify_latest_against_second_source(latest)

    if existing and not full_rebuild and rows == existing:
        print(
            f'no_data_change total={len(rows)} latest=#{latest["draw_number"]} '
            f'winning={latest["winning_number"]}'
        )
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    pretty_history = json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    HISTORY_PATH.write_text(pretty_history, encoding="utf-8")
    history_sha256 = hashlib.sha256(pretty_history.encode("utf-8")).hexdigest()

    generated = datetime.now(timezone.utc).isoformat()
    latest_doc = {
        "schema_version": 1,
        "generated_at_utc": generated,
        "draw": latest,
    }
    LATEST_PATH.write_text(
        json.dumps(latest_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "schema_version": 1,
        "generated_at_utc": generated,
        "source": "Official NLCB Play Whe archive",
        "source_url": BASE_URL,
        "check_results_url": CHECK_RESULTS_URL,
        "integrity_status": "verified" if not gaps else "verified_with_sequence_gaps",
        "total_draws": len(rows),
        "earliest_draw_number": rows[0]["draw_number"],
        "earliest_date": rows[0]["date"],
        "latest_draw_number": latest["draw_number"],
        "latest_date": latest["date"],
        "latest_draw_time": latest["draw_time"],
        "latest_winning_number": latest["winning_number"],
        "history_sha256": history_sha256,
        "sequence_gaps": gaps,
        "pages_scanned": len(page_counts),
        "nonempty_pages": sum(1 for x in page_counts if x > 0),
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f'published total={len(rows)} latest=#{latest["draw_number"]} '
        f'winning={latest["winning_number"]} gaps={len(gaps)}'
    )

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
