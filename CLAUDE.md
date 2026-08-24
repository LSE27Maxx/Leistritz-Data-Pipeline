# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A two-stage GCP data pipeline that gets process-parameter CSV exports from the Leistritz-1 machine's Google Drive folder into BigQuery:

1. **`leistritz-drive-to-gcs-sync`** — Cloud Run service. Polls a Google Drive folder for `.csv` files and copies new ones into a GCS "watch" prefix.
2. **`leistritz-csv-ingest-raw`** — Cloud Functions (Gen2) function. Triggered by GCS object-finalize events on that watch prefix; parses each CSV into long-format rows and loads them into BigQuery.

There is no shared code, build system, or test suite between the two services — each is a standalone deployable with its own `main.py` and `requirements.txt`.

## Deployment

Both services deploy from source (Buildpacks), not from a Dockerfile or CI pipeline — there's no CI config in this repo. Redeploys are done directly against project `notpla-machine-data`.

```bash
# leistritz-drive-to-gcs-sync (Cloud Run, HTTP-triggered)
cd leistritz-drive-to-gcs-sync
gcloud run deploy leistritz-drive-to-gcs-sync --source=. --region=europe-west2

# leistritz-csv-ingest-raw (Cloud Functions Gen2, GCS event-triggered)
cd leistritz-csv-ingest-raw
gcloud functions deploy leistritz-csv-ingest-raw --region=europe-west2 --gen2 \
  --runtime=python311 --entry-point=ingest_csv \
  --trigger-bucket=notpla-machine-data
```

`gcloud run deploy` / `gcloud functions deploy` without extra flags preserve existing env vars, service account, and resource limits from the current revision — only pass flags when intentionally changing config.

There's no local run/test/lint tooling configured (no Makefile, no test files, no linter config). Validate changes by re-deploying and checking Cloud Logging / the BigQuery log table (see below) rather than via a local harness.

## Architecture: the ingest pipeline

The two functions are decoupled through GCS folder prefixes acting as a state machine, not through direct calls:

```
Drive folder (DRIVE_FOLDER_ID)
  --[drive-to-gcs-sync]-->  gs://notpla-machine-data/<WATCH_PREFIX>/*.csv
  --[GCS finalize event]--> csv-ingest-raw
  --[on success]-->  moved to <PROCESSED_PREFIX>/
  --[on failure]-->  moved to <FAILED_PREFIX>/<error_category>/
```

**`leistritz-drive-to-gcs-sync`** (`sync_drive_to_gcs` in `main.py`):
- Lists all non-folder files in `DRIVE_FOLDER_ID`, paginating via `nextPageToken`.
- Dedup is via per-file marker blobs at `<SYNC_STATE_PREFIX>/<drive_file_id>.synced` — a file is "already synced" purely by marker existence, independent of the CSV's ingest outcome downstream.
- Non-`.csv` files are skipped.

**`leistritz-csv-ingest-raw`** (`ingest_csv` in `main.py`), on each GCS finalize event:
- Ignores events whose object path doesn't start with `WATCH_PREFIX` — this also means **the function re-triggers itself harmlessly** when it moves a file to `PROCESSED_PREFIX`/`FAILED_PREFIX` (those copies fire new finalize events that get filtered out here).
- Checks `already_processed()` — a live query against `ingestion_file_log` for a prior `SUCCESS` row for this exact `source_file` path. This is a check-then-act race: concurrent invocations for the same file (GCS delivers at-least-once) can both pass this check before either logs `SUCCESS`.
- CSV parsing rule (`parse_csv_to_long_rows`): row is skipped if column D (index 3) is blank; every other non-empty column becomes one long-format output row, keyed by that column's header as `parameter_name`. Columns B/C (indices 1/2) are the source date/time and are combined into `machine_timestamp`, not emitted as parameters themselves.
- `load_rows_to_bigquery` is a synchronous blocking load (`load_job.result()`) with `WRITE_APPEND`; the `SUCCESS` log write only happens after this returns without error, and only after the source blob has already been moved to `PROCESSED_PREFIX`.
- Every outcome (`SUCCESS`, `FAILED`, `DUPLICATE_REJECTED`) is logged to two places: a row in the `ingestion_file_log` BigQuery table (`write_bq_log`, structured, queryable) and an append-only text blob at `LOG_FILE_PATH` in GCS (`write_text_log`, human-readable running log). Failure `error_category` is inferred from substring-matching the exception message (`"timestamp"`, `"non-numeric"`, `"empty"`, `"bigquery"` → known categories, else `"unknown-error"`) — this is fragile if exception wording changes.

## BigQuery layout (dataset `machine_leistritz_1`)

- `process_parameters_long_raw` — one row per (source_file, source_row_number, parameter_name); append-only, loaded by csv-ingest-raw.
- `ingestion_file_log` — one row per ingest attempt per file (`status` ∈ `SUCCESS`/`FAILED`/`DUPLICATE_REJECTED`; `IGNORED_OUTSIDE_WATCH_FOLDER` was logged historically but the write for it was removed in a later commit). This table is also read at ingest time by `already_processed()`, so it's both an audit log and part of the pipeline's dedup logic.
- `process_parameters_long_reporting` — a view over the raw table adding `source_file_name` (basename of `source_file`) and `elapsed_seconds_from_file_start` (seconds since the earliest `machine_timestamp` for that file, via a window function). Use this view, not the raw table, for reporting/analysis.

## Known gaps to be aware of when touching this code

- `already_processed`'s check-then-act isn't transactional — don't assume duplicate protection is airtight under concurrent triggers.
- `SUCCESS` rows in `ingestion_file_log` are not a guarantee the corresponding rows still exist in `process_parameters_long_raw` today — the log reflects the load job's outcome at ingest time only; anything that later modifies the raw table (manual reload, deletion) isn't reflected back into the log.
- Error categorization in `ingest_csv`'s except block is string-matching on the exception message, not on exception type — new failure modes will fall into `unknown-error` unless the matched keywords happen to appear.
