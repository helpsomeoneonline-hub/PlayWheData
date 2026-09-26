#!/usr/bin/env python3
import json
import sys
from pathlib import Path

DATA = Path("data")
YEAR_DIR = Path("year-data")
OUT = DATA / "historical_history.json"
MANIFEST = DATA / "historical_manifest.json"
CONFLICTS = DATA / "historical_conflicts.json"
OFFICIAL = DATA / "history.json"

def core(r):
    return (int(r["draw_number"]), str(r["date"]), int(r["draw_minutes"]), int(r["winning_number"]))

def main():
    files = sorted(YEAR_DIR.glob("year-*.json"))
    if len(files) < 33:
        raise RuntimeError(f"Expected 33 yearly files (1994-2026), got {len(files)}")

    all_rows = []
    missing_months = []
    for path in files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        all_rows.extend(doc.get("rows", []))
        missing_months.extend(doc.get("missing_months", []))

    by_number = {}
    slots = {}
    for row in all_rows:
        n = int(row["draw_number"])
        old = by_number.get(n)
        if old is not None and core(old) != core(row):
            raise RuntimeError(f"conflicting historical draw #{n}")
        by_number[n] = row
        slot = (row["date"], int(row["draw_minutes"]))
        other = slots.get(slot)
        if other is not None and other != n:
            raise RuntimeError(f"duplicate historical slot {slot}: #{other} and #{n}")
        slots[slot] = n

    historical = sorted(by_number.values(), key=lambda r: int(r["draw_number"]))
    if not historical or int(historical[0]["draw_number"]) != 1:
        raise RuntimeError("Historical archive does not start at draw #1")

    official = []
    if OFFICIAL.exists():
        official = json.loads(OFFICIAL.read_text(encoding="utf-8"))
    official_by = {
        int(r["draw_number"]): r for r in official
        if isinstance(r, dict) and (
            r.get("source_type") == "official_nlcb_rest" or int(r.get("source_post_id") or 0) > 0
        )
    }

    matches = 0
    conflicts = []
    for row in historical:
        o = official_by.get(int(row["draw_number"]))
        if o is None:
            continue
        if core(row) == core(o):
            row["official_cross_checked"] = True
            matches += 1
        else:
            conflicts.append({
                "draw_number": int(row["draw_number"]),
                "historical": {
                    "date": row["date"],
                    "draw_minutes": int(row["draw_minutes"]),
                    "winning_number": int(row["winning_number"]),
                },
                "official": {
                    "date": o["date"],
                    "draw_minutes": int(o["draw_minutes"]),
                    "winning_number": int(o["winning_number"]),
                },
            })

    overlap = matches + len(conflicts)
    if overlap >= 100 and len(conflicts) / overlap > 0.01:
        raise RuntimeError(f"Historical source conflicts too often with official data: {len(conflicts)}/{overlap}")

    numbers = {int(r["draw_number"]) for r in historical}
    gaps = [n for n in range(min(numbers), max(numbers)+1) if n not in numbers]

    OUT.write_text(json.dumps(historical, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    CONFLICTS.write_text(json.dumps(conflicts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": 3,
        "source": "nlcbplaywhelotto.com historical month archive",
        "source_url": "https://www.nlcbplaywhelotto.com/nlcb-play-whe-results/",
        "source_is_official_nlcb": False,
        "earliest_draw_number": int(historical[0]["draw_number"]),
        "earliest_date": historical[0]["date"],
        "latest_draw_number": int(historical[-1]["draw_number"]),
        "latest_date": historical[-1]["date"],
        "total_draws": len(historical),
        "sequence_gap_count": len(gaps),
        "sequence_gaps": gaps,
        "missing_month_count": len(missing_months),
        "missing_months": sorted(missing_months, key=lambda x:(x["year"],x["month"])),
        "official_overlap_count": overlap,
        "official_exact_matches": matches,
        "official_conflict_count": len(conflicts),
        "official_conflict_rate": (len(conflicts)/overlap if overlap else 0.0),
        "note": (
            "Historical rows are sourced from a third-party archive. Official NLCB records "
            "take precedence wherever they overlap. Missing months are source-availability "
            "gaps and are not proof that no draw occurred."
        ),
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"historical_aggregate_complete total={len(historical)} "
        f"earliest=#{manifest['earliest_draw_number']} latest=#{manifest['latest_draw_number']} "
        f"missing_months={len(missing_months)} overlap={overlap} conflicts={len(conflicts)} gaps={len(gaps)}"
    )

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
