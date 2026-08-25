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
  --trigger-bucket=notpla-machine-data \
  --memory=2Gi
```

**Memory:** `leistritz-csv-ingest-raw` runs at **2Gi** (raised from the 1Gi default in Step 142 — the long-format transform plus the pandas DataFrame it builds for the BigQuery load roughly doubles/triples a source CSV's footprint in memory, and files above ~9 MiB were intermittently exceeding 1Gi and crashing with zero log trace). Don't redeploy without `--memory=2Gi` or this regresses silently.

**Automated Drive sync:** a Cloud Scheduler job, `leistritz-drive-to-gcs-sync-daily` (project `notpla-machine-data`, region `europe-west2`), calls `leistritz-drive-to-gcs-sync`'s HTTP endpoint daily at 06:00 Europe/London using OIDC auth as `462425991200-compute@developer.gserviceaccount.com` (already granted `roles/run.invoker` on that service). This satisfies Priority 5 for new files going forward — added in Step 142.

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
- `unresolved_failed_files` — a view (added Step 146) listing distinct files with a `FAILED` row in `ingestion_file_log` but no `SUCCESS` row and no rows in `process_parameters_long_raw` — i.e. genuinely still-broken files, as opposed to the raw `FAILED`-row count which includes resolved race noise and never shrinks. Currently returns 4 rows (`PR1216-retest-2/3/4/5.csv`). Intended as the data source for a Looker Studio scorecard replacing/supplementing the existing lifetime-count one.

## Known gaps to be aware of when touching this code

- `already_processed`'s check-then-act isn't transactional — don't assume duplicate protection is airtight under concurrent triggers.
- `SUCCESS` rows in `ingestion_file_log` are not a guarantee the corresponding rows still exist in `process_parameters_long_raw` today — the log reflects the load job's outcome at ingest time only; anything that later modifies the raw table (manual reload, deletion) isn't reflected back into the log.
- Error categorization in `ingest_csv`'s except block is string-matching on the exception message, not on exception type — new failure modes will fall into `unknown-error` unless the matched keywords happen to appear.
- ~~An empty, typo'd dataset `machine_leistrtiz_1` (should be `machine_leistritz_1`) exists in the project~~ — deleted in Step 147, confirmed empty first.
- Historical failed files `PR1216-retest-2.csv` (non-numeric-data) and `-3`/`-4`/`-5.csv` (unknown-error) were never individually diagnosed — worth investigating if the same failure classes recur.
- BigQuery/gsutil gotchas worth remembering: `rows`/`range`/`groups` are reserved words (alias as `n_rows` etc.); `bq query` without `--max_rows` silently caps at 100 rows; `gsutil` treats square brackets (e.g. a filename containing `[WIP]`) as wildcards, and with `-q` the resulting error is swallowed silently; a `CREATE OR REPLACE VIEW` should be confirmed via its `Replaced ...` output, not assumed to have succeeded.
- Looker Studio (`Leistritz_machine_data` report, page "Process Data (Single ID ONLY...)" — name is a holdover from before multi-file support, page itself now does support multi-file overlay): after changing a data source field's type or a chart/control's filter, the **View mode / published report can serve a stale cached render** even though the Edit-mode canvas already shows the fix. A hard refresh (F5) of the View tab resolves it — don't conclude a fix didn't work, or chase a false regression, without refreshing View mode first. Also watch for: (1) a list/dropdown control whose own current selection is fed back as a page filter can create a self-filtering loop that collapses its own option list down to just the selected value — clear the filter chip in the Filter bar to break the loop; (2) a numeric field (e.g. `elapsed_seconds_from_file_start`) can get auto-typed as Date/Time in the Looker data source rather than Number, producing a `Failed to parse input string` error — fix in Resource → Manage added data sources → Edit, not in the chart config.
- **A burst of many files landing in the watch folder at once (e.g. a bulk Drive sync) can overwhelm the ingest pipeline in two ways, both now understood** (diagnosed and remediated in Step 142): (1) many concurrent Cloud Function invocations hit GCS's per-object mutation rate limit on the single shared `LOG_FILE_PATH` blob and BigQuery's per-table write rate limit — these show up as `FAILED` rows with `429`/`rate limit` in the error message; (2) the documented `already_processed()` race can let two concurrent invocations both pass the check for the same file, and the loser gets a `404 No such object` trying to act on a file the winner already moved. Neither means data was lost — cross-check `source_file` against both other `FAILED`/`SUCCESS` rows for the same file *and* against `process_parameters_long_raw` directly before assuming a file needs retrying, since a `SUCCESS` row can itself fail to write after a real successful load (see next bullet). When genuinely retrying a batch of stuck files, move them back into the watch folder in small batches (~10) with a pause between batches (~30s) rather than all at once, to avoid re-triggering the same storm.
- **A `SUCCESS` write (`write_bq_log`) can itself fail from the same rate limit, after the file's data has already loaded and the blob already moved to `PROCESSED_PREFIX`.** The exception handler then logs a `FAILED` row instead (its own attempt to move the already-moved file 404s, so `destination_path` is often empty) — meaning a `FAILED` row is not proof a file's data is missing. Always check `process_parameters_long_raw` for existing rows under that `source_file` before reprocessing, or you will duplicate its rows. 13 files were left in this state as of Step 142/143 (real data present, no clean `SUCCESS` log) — backfilled with correct `SUCCESS` rows in Step 144, verified `rows_loaded` against the raw table for all 13.
- **Large CSVs (~9 MiB+ source size) can OOM-crash `leistritz-csv-ingest-raw` with zero log trace at all** — the crash kills the container before any log write (BigQuery or GCS text log) completes, so the file just sits in the watch folder indefinitely with no `FAILED` row to explain why. Diagnose via `gcloud logging read` on the Cloud Run revision directly (look for `Memory limit ... exceeded`), not via `ingestion_file_log`. Fixed in Step 142 by raising `--memory` from 1Gi to 2Gi; re-deploying this function without that flag will regress it back to the default and silently reintroduce the failure mode.

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

- **Last completed step:** Step 147 (see step log below).
- **Note:** as of Step 136, the old numbered plan (Steps 1–128b, carried forward from `docs/chatgpt-handover.md` and `docs/session-summary-2026-08-19.md`) is no longer treated as a fixed script to resume in order — see "Source documents and precedence" above. Step numbering continues purely for traceability.
- **In progress:**
  - Step 131 — data discrepancy: `PR1216-retest-7.csv` and `PR1216-retest-8.csv` are logged `SUCCESS` (88,050 rows each) in `ingestion_file_log`, but `process_parameters_long_raw` has zero rows for either. Code trace of `ingest_csv` in `leistritz-csv-ingest-raw/main.py` ruled out a log-before-load-confirmed bug (the BQ load is synchronous and blocks before the success log is written). Root cause not yet confirmed. Note: this predates Step 142's mass-import and is a different, older discrepancy than the "SUCCESS-log-missing-but-data-present" pattern found and left alone in Step 142 (that pattern has a confirmed cause; Step 131 does not).
  - Step 140 follow-up — `Start Time`, `End Time`, `Start Time Seconds`, `End Time Seconds` (parameters) and `In selected time window` (calculated field) in the `process_parameters_long_reporting` data source are now orphaned after the Start Time/End Time control was deleted, but not yet confirmed unused on other report pages (e.g. "File Log") or deleted. Looker Studio will block deletion if still referenced elsewhere, making this a safe cleanup to attempt.
  - Step 142 follow-up — resolved in Step 144: the ~19-file estimate was precisely 13 files, and all 13 now have correct `SUCCESS` rows backfilled into `ingestion_file_log`.
  - Step 143/145 — the 2 genuinely-failed, recoverable files (`AB AC AV AM 250609 GR PR 0656.csv`, `Feeder 1 Big TPS pellets 60kg-h.csv`) were successfully retried and ingested in Step 145. All 6 files from Step 143's "genuinely missing" bucket are now resolved except the 4 unrecoverable `PR1216-retest-2/3/4/5.csv` files (gone from GCS, left as a known gap).
  - Step 146 — Looker's existing "Failed Processing Events" scorecard on the `Leistritz_machine_data` report counts raw `FAILED` rows in `ingestion_file_log` (append-only, lifetime total — currently 270, will never go down since old `FAILED` rows are never deleted). Created a new BigQuery view, `unresolved_failed_files`, giving a true "still broken right now" count (currently 4 — just the `PR1216-retest` files). Not yet wired into a Looker Studio scorecard — that step needs to be done manually in the Looker Studio UI, no CLI/API access available to do it directly.
- **Next action:** open — driven by the priority order above and by what's asked for next, not a fixed continuation. Candidates: user adds the `unresolved_failed_files` view as a Looker Studio data source and places the new scorecard (instructions given, Step 146); confirm and delete the orphaned Start/End Time fields above; address the SME (J/kg)-vs-other-units scale mismatch on the new multi-parameter chart if it becomes a problem (small-multiples split proposed, deferred for now); BigQuery audit-log check for the Step 131 discrepancy; the failure-path test from Working practices (never yet run on this pipeline).
- **Half-finished / open threads:** Step 131 above is open; orphaned Looker fields above pending cleanup; Step 146's scorecard placement in Looker Studio pending (user to do manually).

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
- Step 137 — Visually confirmed the Step 128b Looker Studio overlay chart (report `Leistritz_machine_data`, page 2). Found and fixed three config bugs blocking it: (1) `elapsed_seconds_from_file_start` was mistyped as a Date/Time field in the Looker data source instead of Number, causing a "Failed to parse input string" error; (2) the `source_file_name` list control had a broken filter referencing the `In selected time window` calculated field, leaving its dropdown empty; (3) the line chart itself carried the same broken filter at chart level, returning zero rows. After removing both broken filters and retyping the field, the chart correctly renders one line per selected file (tested with `PR1238.csv` and `PR1237.csv`), all lines starting at `elapsed_seconds_from_file_start` = 0, with plausible values for the filtered parameter (`Extruder speed (rpm)`, ~110-165 range) — done — 2026-08-24
- Step 138 — Fixed the `parameter_name` drop-down control only ever offering one selectable value instead of the full ~50-parameter list. Ruled out a broken filter at chart level, control level, and data-source level (all checked and clean, unlike Step 137's causes). Root cause was a self-filtering loop: the control's own current selection (`Extruder speed (rpm)`) was propagated as a page-level filter chip, which fed back into the control's own available-options query, narrowing it to just the already-selected value. Fix: cleared the active `parameter_name` filter chip from the report's Filter bar, which reset the control back to showing all distinct parameter names and restored multi-select — done — 2026-08-24
- Step 139 — After Step 138's fix, the overlay chart appeared blank in Looker Studio's View mode (tooltip/hover still showed correct underlying values, so data was never the issue). Ruled out Y-axis scaling (already Auto) and a missing `parameter_name` selection. Root cause: View mode was serving a stale cached render of the report that hadn't picked up the data-source field-type and filter fixes from Steps 137-138 yet. Fix: hard-refreshed the View-mode browser tab, which cleared the stale render — chart now renders correctly in both Edit-mode preview and View mode. Documented this caching gotcha, plus the self-filtering-loop and field-mistyping gotchas from Steps 137-138, in Known gaps — done — 2026-08-24
- Step 140 — Replaced the wall-clock Start Time/End Time filter (which compared `Seconds from Midnight`, i.e. absolute time-of-day, via the `In selected time window` CASE formula) with a numeric range slider control bound directly to `elapsed_seconds_from_file_start`. No calculated field needed — Looker Studio's built-in cross-filtering applies the slider's range to every chart on the page automatically. Troubleshot the new slider initially collapsing to a single-point range (311-311): ruled out a control-level filter and the old Start Time/End Time control (deleted, no change), then found and cleared a leftover `In selected time window`-derived selection still sitting as a chip in the report's page-level Filter bar — clearing it revealed the full 0-to-~3690 range. Confirmed working end-to-end: narrowing the slider (e.g. to 1,100-2,700) correctly clips the overlay chart's lines and recalculates the summary table's Average/StDev/Min/Max/CV% to match the selected window. The old `Start Time`/`End Time`/`Start Time Seconds`/`End Time Seconds`/`In selected time window` fields are now orphaned in the data source, not yet deleted — done — 2026-08-24
- Step 141 — Added a second chart for single-run, multi-parameter viewing (complementary to the Step 128b/140 multi-file overlay chart, which uses the opposite breakdown). Duplicated the overlay chart and changed its Breakdown dimension from `source_file_name` to `parameter_name`, keeping `source_file_name` as a filter (pinned to one file) rather than a breakdown. Confirmed working: with 4 parameters selected (Melt Temperature, Melt Pressure, Torque, SME) on one file, all 4 render as separate lines matching the summary table's per-parameter averages. Noted but left unaddressed per user decision: parameters with very different units/scales (e.g. SME ~0.08 J/kg vs Melt Temperature ~130 oC) share one Y-axis, so small-magnitude lines read as visually flat next to large-magnitude ones — a small-multiples split (one chart per unit family) was proposed as the fix if this becomes a problem, deferred for now — done — 2026-08-24
- Step 142 — Imported the full Google Drive backlog into the pipeline and made new-file sync automatic going forward. (1) Triggered `leistritz-drive-to-gcs-sync`; first call hit a 504 at the HTTP layer but kept running server-side, eventually syncing the entire Drive folder (482 CSVs total) into GCS — confirmed complete via a second trigger returning `copied_count: 0`. (2) The resulting burst of ~230 newly-synced files overwhelmed `leistritz-csv-ingest-raw`: 252 `FAILED` log rows appeared, traced to two causes (GCS/BigQuery rate limits from concurrent invocations, and the documented `already_processed()` race) — both now written up in Known gaps. Diagnosed which were real: 66 of 153 distinct failed files also had a genuine `SUCCESS` row (race noise, no action), 16 of the remaining 87 already had raw-table data despite no `SUCCESS` log (the log-write-fails-after-real-success pattern, also written up in Known gaps — left alone, not reprocessed, to avoid duplicating rows), leaving 71 genuinely stuck files. (3) Retried those 71 by moving them from `failed-processing/<category>/` back into the watch folder in batches of 10 with 30s pauses; 70 succeeded automatically, 1 (a filename containing a comma) broke the batch script's naive CSV parsing and was moved manually. (4) 8 files (all 9-13 MiB) still didn't process at all after that — traced via `gcloud logging read` directly against the Cloud Run revision (not `ingestion_file_log`, which had zero entries for them) to an out-of-memory crash: `Memory limit of 1024 MiB exceeded`. Redeployed `leistritz-csv-ingest-raw` with `--memory=2Gi` (revision `leistritz-csv-ingest-raw-00008-zog`), re-triggered all 8 via a two-hop `gsutil mv` (GCS won't move a file to itself directly), and all 8 succeeded (1.38-1.8M rows each). Watch folder is now empty. (5) Created a new Cloud Scheduler job, `leistritz-drive-to-gcs-sync-daily` (06:00 Europe/London, OIDC-authenticated as the already-authorized `462425991200-compute@developer.gserviceaccount.com`), so future Drive files sync automatically without manual triggering — satisfies Priority 5. Final state: 482/482 Drive files synced to GCS; 243 distinct files have real data in `process_parameters_long_raw` (224 with a clean `SUCCESS` log, ~19 with data but a lost log write per the pattern above — cosmetic, not yet backfilled) — done — 2026-08-24
- Step 143 — User reported 270 failed files; reconciled `ingestion_file_log` `FAILED` rows against `SUCCESS` rows and `process_parameters_long_raw` to find out how many were real. The 270 `FAILED` rows (212 `unknown-error`, 57 `bigquery-load-error`, 1 `non-numeric-data`) resolve to 171 distinct files: 152 have a `SUCCESS` row from a later attempt (rate-limit race noise, same pattern as Step 142, no action needed), 13 have real data in `process_parameters_long_raw` but no clean `SUCCESS` log (the lost-log-write pattern — this supersedes Step 142's ~19 estimate with a precise count), and 6 are genuinely missing everywhere. Of those 6, physically checked each against `failed-processing/`: 2 (`AB AC AV AM 250609 GR PR 0656.csv`, `Feeder 1 Big TPS pellets 60kg-h.csv`, both `bigquery-load-error`) are still present there and recoverable — exact `gsutil mv` retry commands given to the user, not yet executed (user's choice, to run themselves). The other 4 are the pre-existing `PR1216-retest-2/3/4/5.csv` failures already noted in Known gaps — confirmed absent from every GCS prefix (`to-be-processed`, `processed`, `failed-processing`), so not recoverable via retry; bucket versioning is Suspended so any prior deletion is permanent. Per user instruction, did not investigate whether these 4 still exist in the source Drive folder. Also scoped (schema-checked `ingestion_file_log` and `process_parameters_long_raw`, confirmed `destination_path` format from a real `SUCCESS` row) an approach for backfilling the 13 cosmetic files with corrected `SUCCESS` rows — done (investigation and scoping); 2-file retry left for the user to execute — 2026-08-25
- Step 144 — Backfilled the 13 cosmetic files identified in Step 143 (real data in `process_parameters_long_raw`, no `SUCCESS` log row) with corrected `SUCCESS` rows in `ingestion_file_log`. Snapshotted the log table first (`ingestion_file_log_presnap_20260825`). Ran an `INSERT ... SELECT` sourcing `rows_loaded` (via `COUNT(*)`) and `processed_at` (via `MAX(processed_at)`) directly from `process_parameters_long_raw` grouped by `source_file`, with `destination_path` derived by swapping `-to-be-processed/` for `-processed/` in `source_file` to match the format of a real `SUCCESS` row. 13 rows inserted; verified every backfilled `rows_loaded` exactly matches the current raw-table row count per file. Did not touch the 2 genuinely-failed files from Step 143 (no raw data exists for those, so nothing to backfill) or the 152 race-noise files (already have a correct `SUCCESS` row) — done — 2026-08-25
- Step 145 — Retried the 2 genuinely-recoverable files identified in Step 143. `gsutil mv`'d both (`AB AC AV AM 250609 GR PR 0656.csv`, `Feeder 1 Big TPS pellets 60kg-h.csv`) from `failed-processing/bigquery-load-error/` back into the watch folder; both re-triggered `leistritz-csv-ingest-raw` automatically and logged `SUCCESS` (70,777 and 92,550 rows respectively). Verified both row counts exactly match `process_parameters_long_raw`, and confirmed the watch folder is empty again afterward. This resolves the last of Step 143's 6 "genuinely missing" files except the 4 unrecoverable `PR1216-retest-2/3/4/5.csv`, which remain a known gap — done — 2026-08-25
- Step 146 — User flagged (via screenshot) that Looker's "Failed Processing Events" scorecard on `Leistritz_machine_data` shows a `Record Count` of 270, unchanged despite Steps 144-145's fixes. Explained why: it's `COUNT(*)` of raw `FAILED` rows in `ingestion_file_log`, an append-only lifetime total that was never going to reflect current pipeline health. Built `unresolved_failed_files`, a new BigQuery view returning distinct files with a `FAILED` row but no `SUCCESS` row and no raw-table data — verified it returns exactly the 4 expected files (`PR1216-retest-2/3/4/5.csv`). Could not wire this into an actual Looker Studio scorecard directly — no CLI/API access to the user's Looker Studio session, only to BigQuery/GCS — so gave the user manual steps to add it as a data source and place the scorecard themselves. The view is ready; the Looker-side scorecard placement is not yet done — done (BigQuery side); Looker scorecard placement pending (user to do) — 2026-08-25
- Step 147 — Cleaned up the typo'd `machine_leistrtiz_1` dataset noted in Known gaps since ~Step 136. Confirmed it held 0 tables and 0 views via `bq ls` before deleting (`bq rm -d -f`), and confirmed afterward that only the correctly-spelled `machine_leistritz_1` remains in the project. User confirmed via AskUserQuestion before deletion — done — 2026-08-25
