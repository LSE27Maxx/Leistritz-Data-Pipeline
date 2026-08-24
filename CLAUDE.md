# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A two-stage GCP data pipeline that gets process-parameter CSV exports from the Leistritz-1 machine's Google Drive folder into BigQuery:

1. **`leistritz-drive-to-gcs-sync`** — Cloud Run service. Polls a Google Drive folder for `.csv` files and copies new ones into a GCS "watch" prefix.
2. **`leistritz-csv-ingest-raw`** — Cloud Functions (Gen2) function. Triggered by GCS object-finalize events on that watch prefix; parses each CSV into long-format rows and loads them into BigQuery.

There is no shared code, build system, or test suite between the two services — each is a standalone deployable with its own `main.py` and `requirements.txt`.

## Source documents and precedence

Three documents describe this project's history and setup, all in `docs/`:

- `docs/chatgpt-handover.md` — earliest handover doc; project objective, architecture overview, step history through ~Step 121, technical-debt checklist.
- `docs/session-summary-2026-08-19.md` — narrower summary of Steps 123–128b (adding the run-relative field to the reporting view, building the Looker overlay chart).
- `docs/claude-code-setup-leistritz.md` — written 2026-08-24 by a colleague (Peter) after directly auditing the deployed state rather than just reading the prior two docs. **This is the most authoritative of the three where they conflict**, because it was verified against live GCP resources.

**Resolved conflict:** the ChatGPT handover recommends naming the run-relative field `run_elapsed_seconds`. That name was never adopted. The actual deployed field — confirmed directly against the live `process_parameters_long_reporting` view — is `elapsed_seconds_from_file_start`. Use that name; see the BigQuery layout section below.

**On the old step-numbered plan (Steps 1–128b and onward):** treat the sequential step numbering and the "Current state"/"Step log" mechanism below as a *traceability discipline*, not a fixed script to execute in order. Picking up the log after Step 128b doesn't mean the Looker-chart-confirmation is mandatorily next — what to work on is driven by the priority order below and by what's actually asked for in a given session.

**Priority order** — settled, don't reorder without being asked:
1. Reliable ingestion
2. Reliable reporting data model
3. Useful Looker dashboard
4. Multi-run comparison
5. Automated Drive synchronisation
6. Production hardening

## Working practices

(from `docs/claude-code-setup-leistritz.md`, Part 4)

- **Permissions:** reading files, `git status`/`git log`, `gcloud ... describe`, `bq show`, and any `SELECT`-only query are safe to run freely. Anything containing `DELETE`, `DROP`, `CREATE OR REPLACE VIEW`, `gsutil rm`, `gsutil mv`, `bq load`, `gcloud functions deploy`, or `gcloud run deploy` needs care. **Bucket versioning on `gs://notpla-machine-data` is Suspended** — a `gsutil rm` is permanent and unrecoverable.
- **Snapshot before changing a table:** `bq cp -n <table> <table>_presnap_YYYYMMDD` before any statement that modifies `process_parameters_long_raw` or another table. Views are cheap to recreate; tables are not.
- **Verify deploys actually took effect** — check the logs for a distinctive string from the new code after any deploy, rather than assuming it landed.
- **Test the failure path, not just the success path** — upload a deliberately malformed CSV, confirm the log records the right `error_category`, the file moves to `failed-processing`, and the raw table's row count is unchanged, then delete the test file. Not yet done on this pipeline as of 2026-08-24.

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
- An empty, typo'd dataset `machine_leistrtiz_1` (should be `machine_leistritz_1`) exists in the project — confirmed empty via `bq ls`, candidate for cleanup.
- Historical failed files `PR1216-retest-2.csv` (non-numeric-data) and `-3`/`-4`/`-5.csv` (unknown-error) were never individually diagnosed — worth investigating if the same failure classes recur.
- BigQuery/gsutil gotchas worth remembering: `rows`/`range`/`groups` are reserved words (alias as `n_rows` etc.); `bq query` without `--max_rows` silently caps at 100 rows; `gsutil` treats square brackets (e.g. a filename containing `[WIP]`) as wildcards, and with `-q` the resulting error is swallowed silently; a `CREATE OR REPLACE VIEW` should be confirmed via its `Replaced ...` output, not assumed to have succeeded.

## Session protocol — progress persistence

This section is an instruction to Claude Code, not a note for the human.

**At the START of every session, before anything else:**

1. Read the "Current state" and "Step log" sections below. State the last
   completed step number and the next action back to me, then wait.
2. Run `gcloud config get-value project` and confirm it returns
   `notpla-machine-data`. If not, run `gcloud config set project notpla-machine-data`.
3. Run `git status` and report anything uncommitted before adding to it.

**After EVERY completed step, without being asked:**

1. Append one line to the "Step log" below, newest last:
   `- Step <n> — <what was done> — <outcome> — <YYYY-MM-DD>`
2. Rewrite the "Current state" block: last step, what is in progress,
   next action, anything left in a half-finished state.
3. Commit and push:
   `git add CLAUDE.md && git commit -m "Step <n>: <summary>" && git push`
4. Confirm the push actually succeeded. If it fails, say so immediately
   and stop — do not continue to the next step on an unpushed log.

**Never batch these updates to the end of a session.** If the session ends
unexpectedly — Cloud Shell disconnect, credit exhaustion, context limit —
anything uncommitted is lost from the record.

**If you are approaching a context or usage limit,** stop what you are doing,
write the state update, commit and push it, and tell me where we are before
continuing with anything else.

**If a step is abandoned rather than completed,** log it as abandoned with the
reason. A gap in the numbering is worse than a recorded dead end.

---

## Current state

- **Last completed step:** Step 136 (see step log below).
- **Note:** as of Step 136, the old numbered plan (Steps 1–128b, carried forward from `docs/chatgpt-handover.md` and `docs/session-summary-2026-08-19.md`) is no longer treated as a fixed script to resume in order — see "Source documents and precedence" above. Step numbering continues purely for traceability.
- **In progress:**
  - Step 128b — the Looker Studio overlay chart (`elapsed_seconds_from_file_start` x-axis, `source_file_name` breakdown) is configured but not yet visually confirmed to render correctly.
  - Step 131 — data discrepancy: `PR1216-retest-7.csv` and `PR1216-retest-8.csv` are logged `SUCCESS` (88,050 rows each) in `ingestion_file_log`, but `process_parameters_long_raw` has zero rows for either. Code trace of `ingest_csv` in `leistritz-csv-ingest-raw/main.py` ruled out a log-before-load-confirmed bug (the BQ load is synchronous and blocks before the success log is written). Root cause not yet confirmed.
- **Next action:** open — driven by the priority order above and by what's asked for next, not a fixed continuation. Candidates: visual confirmation of the Step 128b Looker chart; BigQuery audit-log check for the Step 131 discrepancy; the failure-path test from Working practices (never yet run on this pipeline); cleanup of the `machine_leistrtiz_1` typo dataset.
- **Half-finished / open threads:** Steps 128b and 131 above are both open; neither blocks the other.

---

## Step log

- Step 128b — Looker overlay chart configured, not yet visually confirmed — in progress — 2026-08-19
- Step 129 — Added `.gitignore` for `__pycache__/`/`*.pyc` and untracked the two `.pyc` files already committed to git (in `leistritz-csv-ingest-raw` and `leistritz-drive-to-gcs-sync`) — done — 2026-08-24
- Step 130 — Resolved Step 122 (Drive sync pagination): the local pagination fix had been committed (`b720d73`) but the deployed Cloud Run service was still running the pre-fix build. Redeployed `leistritz-drive-to-gcs-sync` from current repo source via `gcloud run deploy` (revision `leistritz-drive-to-gcs-sync-00003-r5w`); confirmed the newly deployed source matches the repo exactly — done — 2026-08-24
- Step 131 — Investigated `ingestion_file_log` vs `process_parameters_long_raw`: found `PR1216-retest-7.csv` and `PR1216-retest-8.csv` logged `SUCCESS` with 88,050 rows each but absent from the raw table entirely. Traced `ingest_csv`'s control flow; the BQ load is synchronous and blocks before the `SUCCESS` log write, which rules out a log-before-confirm bug in the current code. Root cause is most likely external to the ingest function (manual reload/truncate, or intentional purge of "retest" data) — paused, unconfirmed — 2026-08-24
- Step 132 — Created `CLAUDE.md` documenting project architecture, deploy commands, GCS-prefix state machine, BigQuery layout, and known gaps — done — 2026-08-24
- Step 133 — Added `docs/chatgpt-handover.md` and `docs/session-summary-2026-08-19.md`, converted from user-supplied PDF handover documents — done — 2026-08-24
- Step 134 — Installed this session protocol (Session protocol, Current state, Step log sections) into `CLAUDE.md`, backfilling steps 129–133 for this session's prior uncommitted-to-log work — done — 2026-08-24
- Step 135 — Simulated a fresh session start per protocol: confirmed `gcloud config get-value project` returns `notpla-machine-data` and `git status` is clean with no uncommitted changes — done — 2026-08-24
- Step 136 — Added `docs/claude-code-setup-leistritz.md` (a third source document, written by a colleague after auditing the deployed state). Recorded its precedence over the ChatGPT handover, resolved the `run_elapsed_seconds` vs `elapsed_seconds_from_file_start` naming conflict in its favor, added its Working practices and Priority order to `CLAUDE.md`, expanded Known gaps with its environment traps and cleanup items, and clarified that the old Step 1–128b plan is a traceability log, not a mandatory script to resume in order — done — 2026-08-24
