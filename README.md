# PlayWheData

Public, machine-readable master dataset for **Play Whe Insight**.

## Architecture

Official NLCB Play Whe pages → GitHub Actions validator → this repository → Android app local cache.

The Android app treats this repository as the central source of truth. Phones no longer crawl the entire NLCB archive.

## Data files

- `data/history.json` — complete verified history currently available from the NLCB archive.
- `data/latest.json` — latest independently verified draw.
- `data/manifest.json` — record count, latest draw, SHA-256 and integrity information.

## Validation

Before publishing, the updater checks valid winning numbers (1–36), allowed draw times, duplicate draw IDs, duplicate date/time slots, chronological draw-number order, and independently verifies the newest draw against NLCB's Check Results page.

If validation fails, the workflow exits without publishing changed master data.

This repository contains **public lottery results only**. Never commit Firebase credentials, service-account keys, app signing keys, or other secrets here.
