# Setting up Claude Code for the Leistritz pipeline

For COCO (callum.oconnell@notpla.com)
Prepared 24 August 2026 by Peter, drawing on a full audit and rebuild of the
films tensile, friction and extrusion pipelines in the same GCP project.

---

## What this is and how to use it

This document takes you from no Claude Code to a working, context-aware setup
on the Leistritz pipeline. Work through it in order in your existing Claude
chat window, or just follow it yourself in Cloud Shell.

It has four parts:

- **Part 1** installs Claude Code in Cloud Shell
- **Part 2** establishes ground truth about what is actually deployed, before
  changing anything
- **Part 3** writes the `CLAUDE.md` that gives every future session your
  project context automatically
- **Part 4** covers working practices

**Part 2 matters more than it looks.** On the films pipelines we found that
the deployed Cloud Function source had silently diverged from the git repo for
three months, because a `git push` failed once and nobody noticed. Everything
we reviewed up to that point was reviewed against the wrong code. Part 2 is
how you avoid spending a day on that.

One practical note: your session summary was supplied as an image-only PDF
with no extractable text. Claude Code cannot read those. Everything that
matters needs to be markdown in the repo.

---

## Part 0: before you start

**Prerequisite.** Claude Code requires a Pro, Max, Team, Enterprise or Console
account. The free Claude.ai plan does not include access.

**Two source documents exist for this project and they disagree.**

- `CO_Leistritz_Pipeline_pdf.pdf`, the session summary dated 19 August 2026
- `CO_Leistritz_Data_Pipeline_Handover_ChatGPT.pdf`, the earlier ChatGPT handover

Where they conflict, **the session summary wins**. The most important conflict:
the ChatGPT document recommends naming the run-relative field
`run_elapsed_seconds`. You actually deployed `elapsed_seconds_from_file_start`.
The deployed name is correct and stays. Part 2 includes a check that confirms
this against the live view rather than trusting either document.

---

## Part 1: install Claude Code in Cloud Shell

Cloud Shell is the right place. Your gcloud auth, your bq access and your code
are already there, and the home directory persists between sessions.

### 1.1 Check your PATH

```bash
echo $PATH | tr ':' '\n' | grep -c "$HOME/.local/bin"
```

If that prints `0`, add it. Type this exactly, with the quoting as shown:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
tail -1 ~/.bashrc
```

That last line must print `export PATH="$HOME/.local/bin:$PATH"` with no
surrounding quotes. If it prints the line wrapped in single quotes, remove it
and try again, because bash will fail on every new shell.

### 1.2 Install

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

The native installer needs no Node.js. It places the binary at
`~/.local/bin/claude` and auto-updates in the background.

```bash
exec bash
```

The installer registers Claude Code for new shells, not the one you ran it in.

### 1.3 Verify

```bash
claude --version
claude doctor
```

`claude doctor` prints read-only diagnostics without starting a session. It is
the first thing to run whenever anything misbehaves later.

If you get `command not found`, PATH is the problem. Go back to 1.1.

### 1.4 Authenticate

```bash
claude
```

Follow the browser prompt. Type `/exit` when signed in. This is one-time.

---

## Part 2: establish ground truth

Do this before Claude Code touches anything. It answers three questions you
cannot currently answer with confidence: where the code is, whether the repo
matches what is deployed, and what state the pipeline is actually in.

### 2.1 Find the repo and the uncommitted work

```bash
find ~ -name ".git" -maxdepth 4 -type d 2>/dev/null
```

For whatever it finds:

```bash
cd <repo path>
git remote -v
git log --oneline -10
git status
git log --oneline origin/main..HEAD 2>/dev/null || echo "no upstream tracking"
```

**What to look for.** Anything under `git status` as modified or untracked is
work that exists only on this Cloud Shell VM. The Drive sync pagination fix is
described as written locally but never committed, so it should appear here. If
it does not appear anywhere, it is gone and needs rewriting.

Any commits listed by that last command are local-only and unpushed.

Also check for stray code outside a repo:

```bash
find ~ -name "main.py" -not -path "*/node_modules/*" 2>/dev/null
find ~ -name "*.py" -newermt "2026-05-01" -not -path "*/.local/*" 2>/dev/null | head -30
```

### 2.2 Pull the actually-deployed function source

This is the step that matters most. Do not assume the repo is what is running.

```bash
gcloud config set project notpla-machine-data

mkdir -p ~/groundtruth && cd ~/groundtruth

gcloud functions describe leistritz-csv-ingest-raw \
  --region=europe-west2 --gen2 --format=json > describe.json

python3 - <<'PY'
import json, subprocess
d = json.load(open("describe.json"))
s = d["buildConfig"]["source"]["storageSource"]
uri = f"gs://{s['bucket']}/{s['object']}"
if s.get("generation"):
    uri += f"#{s['generation']}"
print("source archive:", uri)
subprocess.run(["gsutil", "-q", "cp", uri, "deployed_source.zip"], check=True)
PY

unzip -o -q deployed_source.zip -d deployed
ls -la deployed/
```

Now diff it against your repo:

```bash
diff -u <repo path>/<function folder>/main.py ~/groundtruth/deployed/main.py
```

**No output means they match.** Any output is drift, and the deployed version
is the one that has been running. On the films pipelines, two of three
functions had drifted and one had a data-losing bug that existed only in
production.

Do the same for the Drive sync service:

```bash
gcloud run services describe leistritz-drive-to-gcs-sync \
  --region=europe-west2 --format=yaml > drive_sync_describe.yaml
head -40 drive_sync_describe.yaml
```

### 2.3 Confirm the reporting view SQL

The two source documents disagree about the field name. Settle it against the
live view:

```bash
bq show --format=prettyjson \
  notpla-machine-data:machine_leistritz_1.process_parameters_long_reporting \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['view']['query'])"
```

Confirm the field is `elapsed_seconds_from_file_start`. If it is anything else,
the session summary is wrong and needs correcting before you build on it.

### 2.4 Profile the data

```bash
bq query --nouse_legacy_sql --format=prettyjson --max_rows=1000 "
SELECT
  COUNT(*)                              AS n_rows,
  COUNT(DISTINCT source_file)           AS n_files,
  COUNT(DISTINCT parameter_name)        AS n_parameters,
  MIN(machine_timestamp)                AS earliest,
  MAX(machine_timestamp)                AS latest,
  COUNTIF(parameter_value IS NULL)      AS null_values
FROM \`notpla-machine-data.machine_leistritz_1.process_parameters_long_raw\`"
```

Note: `rows` is a reserved word in BigQuery, which is why the alias above is
`n_rows`. This catches people out repeatedly.

Then the ingestion log, which is your best existing asset:

```bash
bq query --nouse_legacy_sql --format=csv --max_rows=1000 "
SELECT status, error_category, COUNT(*) AS n,
       MIN(processed_at) AS first_seen, MAX(processed_at) AS last_seen
FROM \`notpla-machine-data.machine_leistritz_1.ingestion_file_log\`
GROUP BY 1,2 ORDER BY n DESC"
```

### 2.5 Check the folders

```bash
BASE=gs://notpla-machine-data/machine-leistritz-1/machine-leistritz-1-process-parameter-export-files

for f in to-be-processed processed failed-processing; do
  n=$(gsutil ls "$BASE/machine-leistritz-1-process-parameter-export-files-$f/**" 2>/dev/null | grep -ci '\.csv$')
  printf '%-24s %s files\n' "$f" "$n"
done
```

A non-empty `to-be-processed` folder means files are stuck. A populated
`failed-processing` folder is the historical failures listed in your handover
document, which are worth diagnosing now that you have the tooling.

### 2.6 Reconcile the log against BigQuery

This is the check that exposed the largest problem on the films pipelines:
files marked processed whose rows were not actually present.

```bash
bq query --nouse_legacy_sql --format=csv --max_rows=1000 "
WITH logged AS (
  SELECT REGEXP_EXTRACT(source_file, r'[^/]+$') AS f, rows_loaded
  FROM \`notpla-machine-data.machine_leistritz_1.ingestion_file_log\`
  WHERE status = 'SUCCESS'
),
actual AS (
  SELECT REGEXP_EXTRACT(source_file, r'[^/]+$') AS f, COUNT(*) AS n
  FROM \`notpla-machine-data.machine_leistritz_1.process_parameters_long_raw\`
  GROUP BY 1
)
SELECT l.f, l.rows_loaded AS logged_rows, a.n AS actual_rows
FROM logged l LEFT JOIN actual a USING (f)
WHERE a.n IS NULL OR a.n <> l.rows_loaded
ORDER BY l.f"
```

Any row returned is a discrepancy between what the log claims and what the
table holds. An empty result is a clean bill of health and worth recording.

### 2.7 A small cleanup item

The project contains an empty dataset named `machine_leistrtiz_1`, created by
a typo. Worth removing once you have confirmed it holds nothing:

```bash
bq ls machine_leistrtiz_1
```

---

## Part 3: write CLAUDE.md

`CLAUDE.md` sits in your repo root and is read automatically at the start of
every Claude Code session in that repository. It is the mechanism that stops
you re-pasting context.

Create it in the repo you found in step 2.1:

```bash
cd <repo path>
```

Then paste the content from **Appendix A** into `CLAUDE.md`, filling in the
bracketed values from what Part 2 told you. Use the Cloud Shell editor rather
than a terminal heredoc, because pasting long heredocs into a shell mangles
them:

```bash
edit CLAUDE.md
```

Commit it:

```bash
git add CLAUDE.md
git commit -m "Add CLAUDE.md project context for Claude Code"
git push
```

**If that push fails**, fix it now rather than later. GitHub no longer accepts
account passwords for git operations, so you need a personal access token as
the password. Classic token with the `repo` scope, or fine-grained with
Contents set to Read and write. Nothing appears on screen while you paste it
into Cloud Shell; that is normal.

A failed push is exactly how the films repo silently diverged from production
for three months. Do not leave it failing.

Also add the two source documents as markdown so Claude Code can read them:

```bash
mkdir -p docs
# convert or paste the session summary and ChatGPT handover as .md here
git add docs/
git commit -m "Add project handover documents as markdown"
git push
```

---

## Part 4: working practices

### 4.1 Permissions

Claude Code asks before every file write and every command. Start in the
default ask mode and approve individually until you have a feel for it.

- **Approve freely:** reading files, `git status`, `git log`, `gcloud ... describe`,
  `bq show`, and any `SELECT`-only query
- **Read carefully first:** anything containing `DELETE`, `DROP`,
  `CREATE OR REPLACE VIEW`, `gsutil rm`, `gsutil mv`, `bq load`, or
  `gcloud functions deploy` or `gcloud run deploy`

Bucket versioning on `notpla-machine-data` is **Suspended**, so a `gsutil rm`
is permanent and unrecoverable.

### 4.2 Snapshot before you change data

Before any statement that modifies a table:

```bash
bq cp -n notpla-machine-data:machine_leistritz_1.process_parameters_long_raw \
        notpla-machine-data:machine_leistritz_1.process_parameters_long_raw_presnap_YYYYMMDD
```

Views are cheap to recreate; tables are not.

### 4.3 Claude Code has no memory between sessions

It will not recall yesterday's conversation and it will not infer progress by
re-scanning the codebase. `CLAUDE.md` is the persistence mechanism.

**Update the "Current state" section of `CLAUDE.md` and commit after each
completed item, not at the end of a session.** If a session ends unexpectedly,
anything uncommitted is lost from the record. Small frequent commits also suit
your sequential step numbering discipline.

### 4.4 Verify deploys took effect

After any deploy, confirm the new code is running by checking the logs for a
distinctive string from it. Do not assume the deploy landed:

```bash
gcloud functions logs read leistritz-csv-ingest-raw \
  --region=europe-west2 --gen2 --limit=20
```

### 4.5 Test the failure path, not just the success path

A failure path that has never run is not known to work. Upload a deliberately
malformed CSV to the watch folder and confirm three things: the log records
the failure with the right error category, the file moves to
`failed-processing`, and the row count in BigQuery is unchanged. Then delete
the test file.

On the films extrusion pipeline this exact test revealed that the parser
accepted a file containing the literal text `NotAHeader,AlsoNot` and loaded a
row of nulls, reporting success.

---

## Appendix A: CLAUDE.md content

Fill in the bracketed values from Part 2.

````markdown
# CLAUDE.md

Project context for Claude Code. Read automatically at the start of every
session in this repository.

---

## What this project is

The Leistritz extruder process-parameter pipeline.

```
Leistritz extruder
  -> CSV process-parameter export
  -> manual upload to the GCS watch folder
  -> Cloud Function leistritz-csv-ingest-raw (Gen2, Python 3.11)
  -> BigQuery raw table, long format
  -> BigQuery reporting view
  -> Looker Studio, report "Leistritz_machine_data"
```

GCP project `notpla-machine-data`, region `europe-west2`.

A Drive to GCS sync layer exists but is **paused and unreliable**. Manual
upload is the active route.

---

## Working style

- One clear ask per response. Do not stack multiple tasks.
- Succinct answers and questions.
- **Inspect before modifying.** Identify current behaviour, the relevant code,
  dependencies, side effects and expected outcome. Then make the smallest
  sensible change.
- Do not assume a simpler architecture is better. This system was built
  incrementally and its components work.
- If something appears broken, diagnose it before replacing it.
- **Sequential global step numbering** is a core traceability discipline on
  this project. Continue numbering from the last documented step across the
  whole project, not per topic. The last recorded step is 128b.
- Full-file replacements over "find this line" edits where practical.
- Dry run before anything destructive. Snapshot tables before writes.

---

## Resources

| Thing | Value |
|---|---|
| Cloud Function | `leistritz-csv-ingest-raw`, Gen2, Python 3.11, 1 GiB, 540s |
| Function SA | `leistritz-ingest-sa@notpla-machine-data.iam.gserviceaccount.com` |
| Trigger | GCS object finalized |
| Raw table | `machine_leistritz_1.process_parameters_long_raw` |
| Reporting view | `machine_leistritz_1.process_parameters_long_reporting` |
| Ingestion log | `machine_leistritz_1.ingestion_file_log` |
| Bucket | `gs://notpla-machine-data` |
| Watch folder | `machine-leistritz-1/machine-leistritz-1-process-parameter-export-files/machine-leistritz-1-process-parameter-export-files-to-be-processed/` |
| Drive sync | `leistritz-drive-to-gcs-sync` (Cloud Run, paused) |
| Drive sync SA | `leistritz-drive-sync-sa@notpla-machine-data.iam.gserviceaccount.com` |
| Looker report | `Leistritz_machine_data` |

Raw table schema: `source_file`, `processed_at`, `machine_timestamp`,
`source_date`, `source_time`, `source_row_number`, `parameter_name`,
`parameter_value`.

Ingestion log statuses seen so far: `SUCCESS`, `DUPLICATE_REJECTED`,
`IGNORED_OUTSIDE_WATCH_FOLDER`.

---

## Design decisions that are settled

**Long format is intentional.** Do not pivot the raw table into one column per
process parameter. Long format lets arbitrary machine parameters be handled
consistently and makes Looker filtering work.

**The run-relative field is named `elapsed_seconds_from_file_start`.** It lives
in the reporting view, computed as:

```sql
TIMESTAMP_DIFF(
  machine_timestamp,
  MIN(machine_timestamp) OVER (
    PARTITION BY REGEXP_EXTRACT(source_file, r"([^/]+)$")
  ),
  SECOND
) AS elapsed_seconds_from_file_start
```

It is partitioned by the re-derived filename rather than the `source_file_name`
alias, because BigQuery window functions cannot reference a SELECT alias in the
same query.

An earlier handover document recommends the name `run_elapsed_seconds`. That
recommendation was **not** adopted. The deployed name above is correct.

**Duplicate rejection by filename works and is proven.** `PR1216-retest-8.csv`
was processed once and correctly rejected on a second attempt. Do not remove
this behaviour. Whether filename alone is sufficient long term is an open
question; do not change it without understanding the current implementation.

**Time-of-day sliders are superseded.** An earlier statistics implementation
used a "seconds from midnight" helper field. Do not continue developing it.
Time-window statistics should be rebuilt around
`elapsed_seconds_from_file_start` so controls work consistently across files.

---

## Known issues and technical debt

- **Drive sync pagination bug, unresolved.** `leistritz-drive-to-gcs-sync`
  retrieves only the first ~100 of ~245 files in the Shared Drive folder. A fix
  was written locally but never committed, pushed or redeployed. **Do not rely
  on this service.** [Update this line once you confirm whether the local fix
  still exists.]
- **No monitoring or alerting.** Nothing notifies anyone when a file lands in
  the failed-processing folder.
- **The reporting view has no data quality filtering.** It is a passthrough of
  the raw table plus two derived columns. Nulls, malformed rows and duplicate
  ingestions are not filtered and this has not been explicitly tested.
- **No automated tests.** Changes are verified manually against PR1238.csv.
- **Historical failed files not diagnosed:** `PR1216-retest-2.csv`
  (non-numeric-data), `-3`, `-4`, `-5` (unknown-error).
- **No retention or lifecycle policy** on the bucket or the BigQuery tables.
- **Bucket versioning is Suspended.** Any delete is permanent.

---

## Environment traps

Each of these has caused a silent failure or a wasted session on pipelines in
this project.

1. **Cloud Shell silently loses GCP project context.** If a `bq` or `gcloud`
   command fails with "Cannot start a job without a project id", run
   `gcloud config set project notpla-machine-data`. Verify at the start of every
   session with `gcloud config get-value project`.
2. **"Regional Access Boundary / Gaia id not found"** appears intermittently in
   Cloud Shell. Verify auth with `gcloud auth list` and project context before
   assuming it is the root cause. It is usually background noise.
3. **Always confirm a `CREATE OR REPLACE VIEW` actually executed.** Check for
   the `Replaced ...` confirmation. An earlier failed job can leave the view
   unchanged while a later step assumes it succeeded.
4. **`rows`, `range` and `groups` are reserved words in BigQuery.** Alias as
   `n_rows`.
5. **`bq query` defaults to 100 rows** unless `--max_rows` is set. A comparison
   against a large table will silently compare against 100.
6. **`bq` returns non-JSON output** for DDL and DML. Parse defensively.
7. **`gsutil` treats square brackets as wildcards.** `[WIP]` matches W, I or P.
   With `-q` the error is swallowed entirely.
8. **`gsutil -m cp -I` drops most of stdin.** Use wildcard copy and prune locally.
9. **`while read` drops the final line** if the file has no trailing newline.
10. **Pasting heredocs into a terminal mangles them.** Use the editor.
11. **A failed `git push` caused three months of undetected repo divergence** on
    a sibling pipeline. Confirm every push succeeded.

**General principle: assert expected counts at every step.** Do not trust that
a loop consumed everything or that a query returned everything.

---

## Deploy and verify

```bash
cd <repo>/<function folder>
python3 -m py_compile main.py && echo "compiles clean"
gcloud functions deploy leistritz-csv-ingest-raw \
  --region=europe-west2 --gen2 --source=. --quiet
```

Then confirm the new code is running by checking the logs for a distinctive
string from it. Do not assume the deploy landed.

Commit and push in the same session as any deploy.

---

## Priority order

Settled, do not reorder without being asked:

1. Reliable ingestion
2. Reliable reporting data model
3. Useful Looker dashboard
4. Multi-run comparison
5. Automated Drive synchronisation
6. Production hardening

Do not prioritise Drive sync over reporting and data-model work.

---

## Current state

**Last step: 128b, in progress.**

The reporting view has `elapsed_seconds_from_file_start`, verified against
PR1238.csv showing 0 at the first timestamp. Multiple rows at 0 seconds is
correct and reflects multiple `parameter_name` rows sharing the first
`machine_timestamp`.

The Looker overlay chart is configured but **not yet visually confirmed**:

- Chart type: time series or line
- Dimension: `elapsed_seconds_from_file_start`
- Breakdown: `source_file_name`, producing one line per file
- Metric: `parameter_value`
- Sort: `elapsed_seconds_from_file_start` ascending
- Filter, required: `parameter_name`, otherwise all parameters mix into one metric

**Next action:** confirm the chart renders correctly, with lines starting at 0
and one line per selected file.

**Resume when appropriate:** Step 122, the Drive sync pagination fix (commit,
push, redeploy).

Confirmed working test file: PR1238.csv, 156,550 rows,
2026-06-08 17:45:57 to 18:38:07.

[Update this section and commit after each completed step.]
````

---

## Appendix B: a good first session

Start read-only so you can watch how it behaves before it changes anything.

```bash
cd <repo path>
claude
```

A good opening prompt:

> Read CLAUDE.md and the documents in docs/. Then run the ground truth checks
> in Part 2 of the setup guide: diff the deployed Cloud Function source against
> this repo, confirm the reporting view field name, and reconcile the ingestion
> log against the raw table. Report what you find. Do not change anything yet.

That produces the facts you need with nothing written, and it shows you how
Claude Code handles context before you give it anything destructive to do.

Useful in-session commands: `/help`, `/model` to switch model, `/clear` to
start fresh, `/exit` to leave.

---

## Appendix C: what the films pipelines learned

Offered as pattern rather than prescription. Your pipeline is separate and
should stay separate until it is working the way you want.

Things your pipeline already does better than the films ones did:

- **A real ingestion log with error categorisation.** The films pipelines had
  nothing equivalent, which is why 217 failed files accumulated unnoticed for
  five months.
- **Long format storage.** The films tensile and friction tables are wide, and
  making them comparable now requires a union view.
- **A dedicated least-privilege service account.** The three films functions
  run as the compute default account, which holds `roles/editor` on the project.
- **Proven duplicate rejection.**

Things worth borrowing later:

- **Alerting.** A log-based metric on a distinctive failure string, plus a
  Monitoring alert policy to an email channel, took about ten minutes to build
  and works. Verify the notification channel in the console; email channels do
  not deliver until verified.
- **A row-errors table.** Loading good rows and capturing bad ones with a
  reason beats rejecting a whole file for one bad row.
- **Pipeline health in Looker.** A page showing files processed per day, current
  failed count, and last-seen date per pipeline is far more accessible than
  expecting anyone to query a log table.
- **Never key on anything an operator can retype.** On the films tensile data,
  sample numbers were rewritten by two separate manual Excel backfills, which
  made an entire reconciliation report 1,020 missing specimens when the true
  figure was 52.

When both pipelines are stable there is a case for a shared manifest table, a
shared alerting mechanism and a shared parsing library across all instruments.
That is a later conversation and should not block your current work.
