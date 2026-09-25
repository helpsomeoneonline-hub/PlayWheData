#!/usr/bin/env python3
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("data")
MASTER = DATA / "history.json"
HISTORICAL = DATA / "historical_history.json"
HIST_MANIFEST = DATA / "historical_manifest.json"
CONFLICTS = DATA / "historical_conflicts.json"
MANIFEST = DATA / "manifest.json"
LATEST = DATA / "latest.json"

ALLOWED_TIMES = {630, 780, 960, 1140}

def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))

def normalize(row):
    x = dict(row)
    x["draw_number"] = int(x["draw_number"])
    x["date"] = str(x["date"])
    x["draw_time"] = str(x["draw_time"])
    x["draw_minutes"] = int(x["draw_minutes"])
    x["winning_number"] = int(x["winning_number"])
    if not x.get("source_type"):
        x["source_type"] = (
            "official_nlcb_rest" if int(x.get("source_post_id") or 0) > 0
            else "legacy_master"
        )
    return x

def is_official(row):
    return row.get("source_type") == "official_nlcb_rest" or int(row.get("source_post_id") or 0) > 0

def core(row):
    return (row["date"], row["draw_minutes"], row["winning_number"])

def main():
    current = [normalize(r) for r in read_json(MASTER, []) if isinstance(r, dict)]
    historical = [normalize(r) for r in read_json(HISTORICAL, []) if isinstance(r, dict)]
    if not historical:
        raise RuntimeError("Historical history is not available yet.")

    official = [r for r in current if is_official(r)]
    if not official:
        raise RuntimeError("Official NLCB master rows are not available.")

    official_by = {r["draw_number"]: r for r in official}
    combined_by = {}
    overlap_matches = 0
    overlap_conflicts = []

    for row in historical:
        combined_by[row["draw_number"]] = row

    for row in official:
        old = combined_by.get(row["draw_number"])
        if old is not None:
            if core(old) == core(row):
                overlap_matches += 1
            else:
                overlap_conflicts.append({
                    "draw_number": row["draw_number"],
                    "historical": {
                        "date": old["date"],
                        "draw_minutes": old["draw_minutes"],
                        "winning_number": old["winning_number"],
                    },
                    "official": {
                        "date": row["date"],
                        "draw_minutes": row["draw_minutes"],
                        "winning_number": row["winning_number"],
                    },
                })
        combined_by[row["draw_number"]] = row

    combined = sorted(
        combined_by.values(),
        key=lambda r: (r["date"], r["draw_minutes"], r["draw_number"]),
    )

    seen_slots = {}
    seen_numbers = set()
    previous_number = None
    for row in combined:
        n = row["draw_number"]
        if n in seen_numbers:
            raise RuntimeError(f"Duplicate draw number #{n}.")
        seen_numbers.add(n)
        if row["winning_number"] not in range(1, 37):
            raise RuntimeError(f"Invalid winning number for draw #{n}.")
        if row["draw_minutes"] not in ALLOWED_TIMES:
            raise RuntimeError(f"Invalid draw time for draw #{n}.")
        slot = (row["date"], row["draw_minutes"])
        if slot in seen_slots and seen_slots[slot] != n:
            raise RuntimeError(
                f"Duplicate date/time slot {slot}: #{seen_slots[slot]} and #{n}."
            )
        seen_slots[slot] = n
        if previous_number is not None and n <= previous_number:
            raise RuntimeError(
                f"Chronology mismatch: draw #{previous_number} then draw #{n}."
            )
        previous_number = n

    min_n = min(seen_numbers)
    max_n = max(seen_numbers)
    gaps = [n for n in range(min_n, max_n + 1) if n not in seen_numbers]

    source_counts = {}
    for row in combined:
        source = row.get("source_type") or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1

    history_text = json.dumps(combined, ensure_ascii=False, indent=2) + "\n"
    history_sha = hashlib.sha256(history_text.encode("utf-8")).hexdigest()
    MASTER.write_text(history_text, encoding="utf-8")

    latest = combined[-1]
    generated = datetime.now(timezone.utc).isoformat()
    LATEST.write_text(
        json.dumps({
            "schema_version": 3,
            "generated_at_utc": generated,
            "draw": latest,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    hist_manifest = read_json(HIST_MANIFEST, {})
    prior_manifest = read_json(MANIFEST, {})
    conflicts_from_backfill = read_json(CONFLICTS, [])

    manifest = {
        "schema_version": 3,
        "generated_at_utc": generated,
        "integrity_status": (
            "validated_mixed_sources_with_gaps" if gaps
            else "validated_mixed_sources"
        ),
        "total_draws": len(combined),
        "earliest_draw_number": min_n,
        "earliest_date": combined[0]["date"],
        "latest_draw_number": latest["draw_number"],
        "latest_date": latest["date"],
        "latest_draw_time": latest["draw_time"],
        "latest_winning_number": latest["winning_number"],
        "history_sha256": history_sha,
        "source_counts": source_counts,
        "official_source": "Official NLCB Play Whe WordPress REST API",
        "official_source_api_url": "https://www.nlcbgames.com/wp-json/wp/v2/play-whe-result",
        "historical_source": "nlcbplaywhelotto.com month archive",
        "historical_source_url": "https://www.nlcbplaywhelotto.com/nlcb-play-whe-results/",
        "historical_source_is_official_nlcb": False,
        "historical_official_overlap_matches": overlap_matches,
        "historical_official_overlap_conflicts": len(overlap_conflicts),
        "backfill_reported_conflicts": len(conflicts_from_backfill),
        "sequence_gap_count": len(gaps),
        "sequence_gaps": gaps,
        "historical_manifest": hist_manifest,
        "official_previous_manifest_generated_at_utc": prior_manifest.get("generated_at_utc"),
        "provenance_note": (
            "Official NLCB records take precedence wherever sources overlap. "
            "Older rows that are unavailable from the official REST archive are retained "
            "from nlcbplaywhelotto.com and are explicitly marked with source_type."
        ),
    }
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f"combined_master_complete total={len(combined)} "
        f"earliest=#{min_n} latest=#{max_n} "
        f"official={source_counts.get('official_nlcb_rest',0)} "
        f"historical={source_counts.get('nlcbplaywhelotto_archive',0)} "
        f"overlap_matches={overlap_matches} "
        f"overlap_conflicts={len(overlap_conflicts)} gaps={len(gaps)}"
    )

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
