#!/usr/bin/env python3
"""
Build the central Play Whe master dataset from NLCB's official WordPress REST API.

Normal scheduled runs fetch only the newest records and merge them into the
existing GitHub master. FULL_REBUILD=true fetches every REST page and performs
a complete reconciliation audit.
"""
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

API_URL = "https://www.nlcbgames.com/wp-json/wp/v2/play-whe-result"
SITEMAP_URL = "https://www.nlcbgames.com/play-whe-result-sitemap.xml"
CHECK_RESULTS_URL = "https://www.nlcbgames.com/check-results/"
CHECK_RESULTS_RELAY = "https://r.jina.ai/http://www.nlcbgames.com/check-results/"
DATA_DIR = Path("data")
HISTORY_PATH = DATA_DIR / "history.json"
LATEST_PATH = DATA_DIR / "latest.json"
MANIFEST_PATH = DATA_DIR / "manifest.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 PlayWheData/2.0",
    "Accept": "application/json,text/plain,text/html,application/xhtml+xml",
}
REST_FIELDS = "id,slug,date,acf"
PER_PAGE = 100

# These are NLCB's current ACF values. We map them to the advertised draw time.
ACF_TIME_MAP = {
    "10:25:00": ("10:30 AM", 630),
    "12:55:00": ("1:00 PM", 780),
    "15:55:00": ("4:00 PM", 960),
    "18:55:00": ("7:00 PM", 1140),
}
TIME_ORDER = {
    "10:30 AM": 0,
    "1:00 PM": 1,
    "4:00 PM": 2,
    "7:00 PM": 3,
}

def request(url, *, params=None, timeout=45, attempts=4):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 6))
    raise RuntimeError(f"Request failed after {attempts} attempts: {url}: {last}")

def parse_draw_date(raw):
    value = str(raw or "").strip()
    for fmt in ("%m/%d/%Y", "%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise RuntimeError(f"Unknown NLCB draw_date format: {value!r}")

def parse_rest_post(post):
    slug = str(post.get("slug") or "").strip()
    if not re.fullmatch(r"\d{6,12}", slug):
        return None

    draw_number = int(slug)
    acf = post.get("acf") or {}
    date = parse_draw_date(acf.get("draw_date"))
    raw_time = str(acf.get("draw_time") or "").strip()
    if raw_time not in ACF_TIME_MAP:
        raise RuntimeError(
            f"Draw #{draw_number} has unknown NLCB draw_time {raw_time!r}; "
            "refusing to guess."
        )
    draw_time, draw_minutes = ACF_TIME_MAP[raw_time]

    winning_group = acf.get("play_whe_winning_numbers") or {}
    try:
        winning_number = int(winning_group.get("winning_number"))
    except Exception:
        raise RuntimeError(f"Draw #{draw_number} is missing a numeric winning number.")
    if winning_number not in range(1, 37):
        raise RuntimeError(
            f"Draw #{draw_number} has invalid winning number {winning_number}."
        )

    return {
        "draw_number": draw_number,
        "date": date,
        "draw_time": draw_time,
        "draw_minutes": draw_minutes,
        "winning_number": winning_number,
        "source_url": f"https://www.nlcbgames.com/play-whe-result/{slug}/",
        "source_post_id": int(post.get("id") or 0),
        "source_published_at": str(post.get("date") or ""),
    }

def fetch_rest_page(page, per_page=PER_PAGE, order="asc"):
    params = {
        "per_page": per_page,
        "page": page,
        "orderby": "id",
        "order": order,
        "_fields": REST_FIELDS,
    }
    r = request(API_URL, params=params)
    posts = r.json()
    if not isinstance(posts, list):
        raise RuntimeError(f"NLCB REST page {page} did not return a list.")
    rows = []
    skipped = []
    for post in posts:
        row = parse_rest_post(post)
        if row is None:
            skipped.append(str(post.get("slug")))
        else:
            rows.append(row)
    return {
        "page": page,
        "rows": rows,
        "skipped": skipped,
        "total_posts": int(r.headers.get("X-WP-Total", len(posts))),
        "total_pages": int(r.headers.get("X-WP-TotalPages", 1)),
    }

def dedupe_rows(rows):
    by_number = {}
    duplicate_posts = []
    for row in rows:
        number = row["draw_number"]
        old = by_number.get(number)
        if old is None:
            by_number[number] = row
            continue

        old_core = (
            old["date"], old["draw_time"], old["draw_minutes"], old["winning_number"]
        )
        new_core = (
            row["date"], row["draw_time"], row["draw_minutes"], row["winning_number"]
        )
        if old_core != new_core:
            raise RuntimeError(
                f"Conflicting official posts for draw #{number}: "
                f"{old_core} vs {new_core}"
            )
        duplicate_posts.append({
            "draw_number": number,
            "kept_post_id": old.get("source_post_id"),
            "duplicate_post_id": row.get("source_post_id"),
        })

    result = sorted(
        by_number.values(),
        key=lambda d: (d["date"], TIME_ORDER[d["draw_time"]], d["draw_number"]),
    )
    return result, duplicate_posts

def fetch_full_history():
    first = fetch_rest_page(1, PER_PAGE, "asc")
    total_pages = first["total_pages"]
    total_posts = first["total_posts"]
    print(f"REST total_posts={total_posts} total_pages={total_pages}")

    pages = {1: first}
    if total_pages > 1:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(fetch_rest_page, page, PER_PAGE, "asc"): page
                for page in range(2, total_pages + 1)
            }
            for future in as_completed(futures):
                result = future.result()
                pages[result["page"]] = result
                print(
                    f'page={result["page"]}/{total_pages} '
                    f'rows={len(result["rows"])} skipped={len(result["skipped"])}'
                )

    all_rows = []
    skipped = []
    for page in range(1, total_pages + 1):
        all_rows.extend(pages[page]["rows"])
        skipped.extend(pages[page]["skipped"])

    rows, duplicates = dedupe_rows(all_rows)
    return rows, {
        "source_total_posts": total_posts,
        "rest_pages_scanned": total_pages,
        "skipped_non_draw_slugs": sorted(set(skipped)),
        "duplicate_source_posts": duplicates,
    }

def fetch_recent_history():
    result = fetch_rest_page(1, 40, "desc")
    rows, duplicates = dedupe_rows(result["rows"])
    return rows, {
        "source_total_posts": result["total_posts"],
        "rest_pages_scanned": 1,
        "skipped_non_draw_slugs": sorted(set(result["skipped"])),
        "duplicate_source_posts": duplicates,
    }

def load_existing_history():
    if not HISTORY_PATH.exists():
        return []
    try:
        value = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except Exception:
        return []

def normalize_existing(rows):
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            normalized.append({
                "draw_number": int(row["draw_number"]),
                "date": str(row["date"]),
                "draw_time": str(row["draw_time"]),
                "draw_minutes": int(row["draw_minutes"]),
                "winning_number": int(row["winning_number"]),
                "source_url": str(row.get("source_url") or ""),
                "source_post_id": int(row.get("source_post_id") or 0),
                "source_published_at": str(row.get("source_published_at") or ""),
            })
        except Exception:
            raise RuntimeError("Existing GitHub history contains an unreadable row.")
    return normalized

def merge_rows(existing, fresh):
    by_number = {row["draw_number"]: row for row in existing}
    for row in fresh:
        old = by_number.get(row["draw_number"])
        if old is not None:
            old_core = (
                old["date"], old["draw_time"], old["draw_minutes"], old["winning_number"]
            )
            new_core = (
                row["date"], row["draw_time"], row["draw_minutes"], row["winning_number"]
            )
            if old_core != new_core:
                # NLCB can correct a posted result. Accept only because this fresh row
                # came directly from the official REST record, and record the change
                # through Git history.
                print(
                    f'official_correction draw=#{row["draw_number"]} '
                    f'old={old_core} new={new_core}'
                )
        by_number[row["draw_number"]] = row

    return sorted(
        by_number.values(),
        key=lambda d: (d["date"], TIME_ORDER[d["draw_time"]], d["draw_number"]),
    )

def validate_history(rows):
    if not rows:
        raise RuntimeError("Master history is empty.")

    seen_slots = {}
    seen_numbers = set()
    issues = []

    for row in rows:
        n = row["draw_number"]
        if n in seen_numbers:
            issues.append(f"duplicate draw number #{n}")
        seen_numbers.add(n)

        if row["winning_number"] not in range(1, 37):
            issues.append(f"invalid winning number for #{n}")
        if row["draw_minutes"] not in (630, 780, 960, 1140):
            issues.append(f"invalid draw time for #{n}")

        slot = (row["date"], row["draw_minutes"])
        if slot in seen_slots and seen_slots[slot] != n:
            issues.append(
                f"duplicate date/time slot {slot}: "
                f'#{seen_slots[slot]} and #{n}'
            )
        seen_slots[slot] = n

    chronological = sorted(
        rows,
        key=lambda d: (d["date"], d["draw_minutes"], d["draw_number"]),
    )
    for a, b in zip(chronological, chronological[1:]):
        if b["draw_number"] <= a["draw_number"]:
            issues.append(
                f'non-increasing chronology: #{a["draw_number"]} then #{b["draw_number"]}'
            )
            break

    numbers = sorted(seen_numbers)
    sequence_gaps = []
    for a, b in zip(numbers, numbers[1:]):
        if b > a + 1:
            sequence_gaps.append({
                "after": a,
                "before": b,
                "missing_draw_numbers": list(range(a + 1, b)),
            })

    if issues:
        raise RuntimeError("Master integrity validation failed: " + "; ".join(issues[:10]))

    # Do not fabricate records that are absent from NLCB's official REST source.
    # Historical source gaps are published explicitly in the manifest. A gap near
    # the newest draws is different: that may mean the current scrape is incomplete,
    # so we fail closed and wait rather than silently publish a bad current dataset.
    latest_number = max(seen_numbers)
    recent_gap_floor = latest_number - 40
    recent_gaps = [
        g for g in sequence_gaps
        if any(n >= recent_gap_floor for n in g["missing_draw_numbers"])
    ]
    if recent_gaps:
        preview = "; ".join(
            f'{g["after"]}->{g["before"]}' for g in recent_gaps[:10]
        )
        raise RuntimeError(
            "Recent official REST history has missing draw-number sequence(s): " + preview
        )

    if sequence_gaps:
        preview = "; ".join(
            f'{g["after"]}->{g["before"]}' for g in sequence_gaps[:10]
        )
        print("historical_source_gaps=" + preview)

    return chronological, sequence_gaps

def fetch_check_result_lines():
    # Try NLCB directly first. If its main site throttles GitHub's IP range, use
    # a text rendering relay while still reading the same official NLCB page.
    try:
        r = request(CHECK_RESULTS_URL, timeout=20, attempts=2)
        soup = BeautifulSoup(r.text, "html.parser")
        lines = [re.sub(r"\s+", " ", s).strip() for s in soup.stripped_strings if s.strip()]
        if lines:
            return lines, "direct"
    except Exception as exc:
        print(f"check_results_direct_failed={exc}")

    r = request(CHECK_RESULTS_RELAY, timeout=40, attempts=3)
    lines = [re.sub(r"\s+", " ", s).strip() for s in r.text.splitlines() if s.strip()]
    return lines, "relay"

def cross_check_latest(latest):
    lines, method = fetch_check_result_lines()
    target = latest["draw_number"]
    draw_pat = re.compile(rf"Draw#\s*0*{target}\b", re.I)

    for i, line in enumerate(lines):
        if not draw_pat.search(line):
            continue

        saw_winning = False
        for candidate in lines[i + 1:i + 16]:
            low = candidate.lower()
            if "jackpot" in low:
                break
            if "winning number" in low:
                saw_winning = True
                continue
            if not saw_winning:
                continue
            m = re.search(r"(?<!\d)(\d{1,2})(?!\d)", candidate)
            if m:
                value = int(m.group(1))
                if 1 <= value <= 36:
                    if value != latest["winning_number"]:
                        raise RuntimeError(
                            f'Latest draw cross-check mismatch for #{target}: '
                            f'REST={latest["winning_number"]}, Check Results={value}'
                        )
                    print(f"latest_cross_check=passed method={method} draw=#{target}")
                    return

    raise RuntimeError(
        f"Latest draw #{target} could not be verified on NLCB Check Results."
    )

def check_sitemap_latest(latest):
    # Lightweight structural check: current REST latest must appear in NLCB's
    # Play Whe sitemap. This helps detect a REST/sitemap publishing mismatch.
    r = request(SITEMAP_URL, timeout=30, attempts=3)
    expected = f"/play-whe-result/{latest['draw_number']:09d}/"
    if expected not in r.text:
        raise RuntimeError(
            f"Latest REST draw #{latest['draw_number']} is absent from NLCB sitemap."
        )
    print(f"sitemap_cross_check=passed draw=#{latest['draw_number']}")

def write_master(rows, source_meta, sequence_gaps):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    latest = rows[-1]
    generated = datetime.now(timezone.utc).isoformat()

    history_text = json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    history_sha256 = hashlib.sha256(history_text.encode("utf-8")).hexdigest()

    HISTORY_PATH.write_text(history_text, encoding="utf-8")
    LATEST_PATH.write_text(
        json.dumps({
            "schema_version": 2,
            "generated_at_utc": generated,
            "draw": latest,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "schema_version": 2,
        "generated_at_utc": generated,
        "source": "Official NLCB Play Whe WordPress REST API",
        "source_api_url": API_URL,
        "source_sitemap_url": SITEMAP_URL,
        "check_results_url": CHECK_RESULTS_URL,
        "integrity_status": (
            "verified_with_source_gaps" if sequence_gaps else "verified"
        ),
        "total_draws": len(rows),
        "source_total_posts": source_meta.get("source_total_posts"),
        "earliest_draw_number": rows[0]["draw_number"],
        "earliest_date": rows[0]["date"],
        "latest_draw_number": latest["draw_number"],
        "latest_date": latest["date"],
        "latest_draw_time": latest["draw_time"],
        "latest_winning_number": latest["winning_number"],
        "history_sha256": history_sha256,
        "rest_pages_scanned": source_meta.get("rest_pages_scanned"),
        "skipped_non_draw_slugs": source_meta.get("skipped_non_draw_slugs", []),
        "duplicate_source_posts": source_meta.get("duplicate_source_posts", []),
        "sequence_gaps": sequence_gaps,
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

def main():
    existing = normalize_existing(load_existing_history())
    full_rebuild = os.environ.get("FULL_REBUILD", "").lower() == "true" or not existing

    if full_rebuild:
        print("mode=full_rebuild_rest")
        rows, source_meta = fetch_full_history()
    else:
        print("mode=incremental_rest")
        fresh, source_meta = fetch_recent_history()
        rows = merge_rows(existing, fresh)

    rows, sequence_gaps = validate_history(rows)
    latest = rows[-1]

    check_sitemap_latest(latest)
    cross_check_latest(latest)

    # Compare the factual core, ignoring generated timestamps and metadata.
    if existing:
        old_core = [
            (r["draw_number"], r["date"], r["draw_minutes"], r["winning_number"])
            for r in existing
        ]
        new_core = [
            (r["draw_number"], r["date"], r["draw_minutes"], r["winning_number"])
            for r in rows
        ]
        if old_core == new_core:
            print(
                f'no_data_change total={len(rows)} '
                f'latest=#{latest["draw_number"]} winning={latest["winning_number"]}'
            )
            return

    write_master(rows, source_meta, sequence_gaps)
    print(
        f'published total={len(rows)} latest=#{latest["draw_number"]} '
        f'winning={latest["winning_number"]} '
        f'raw_posts={source_meta.get("source_total_posts")}'
    )

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
