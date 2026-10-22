-- process_parameters_long_reporting (view) -- dropped 2026-10-22 (Step 166 in CLAUDE.md).
-- Replaced by the physical table process_parameters_long_reporting_optimised (Step 157).
-- Kept here so it can be recreated if ever needed:
--   CREATE VIEW `notpla-machine-data.machine_leistritz_1.process_parameters_long_reporting` AS <query below>
-- WARNING: querying this view scans the full raw table via a window function (the cause of the
-- Aug 2026 cost incident). Do not point Looker Studio at it.

WITH base AS (
  SELECT
    REGEXP_EXTRACT(source_file, r"([^/]+)$") AS source_file_name,
    source_file,
    processed_at,
    machine_timestamp,
    source_date,
    source_time,
    source_row_number,
    parameter_name,
    parameter_value,
    TIMESTAMP_DIFF(
      machine_timestamp,
      MIN(machine_timestamp) OVER (PARTITION BY REGEXP_EXTRACT(source_file, r"([^/]+)$")),
      SECOND
    ) AS elapsed_seconds_from_file_start
  FROM `notpla-machine-data.machine_leistritz_1.process_parameters_long_raw`
)
SELECT
  *,
  CAST(FLOOR(elapsed_seconds_from_file_start / 10) * 10 AS INT64) AS elapsed_seconds_bucketed_10s
FROM base
