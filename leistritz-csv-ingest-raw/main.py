import os
import io
import csv
import traceback
from datetime import datetime, timezone

import chardet
import pandas as pd
from google.cloud import storage
from google.cloud import bigquery


PROJECT_ID = os.environ["PROJECT_ID"]
BQ_DATASET = os.environ["BQ_DATASET"]
BQ_TABLE = os.environ["BQ_TABLE"]
BQ_LOG_TABLE = os.environ["BQ_LOG_TABLE"]

WATCH_PREFIX = os.environ["WATCH_PREFIX"]
PROCESSED_PREFIX = os.environ["PROCESSED_PREFIX"]
FAILED_PREFIX = os.environ["FAILED_PREFIX"]
LOG_FILE_PATH = os.environ["LOG_FILE_PATH"]

storage_client = storage.Client()
bq_client = bigquery.Client(project=PROJECT_ID)


def write_text_log(bucket_name, message):
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(LOG_FILE_PATH)

    timestamp = datetime.now(timezone.utc).isoformat()
    new_line = f"{timestamp} | {message}\n"

    try:
        existing = blob.download_as_text()
    except Exception:
        existing = ""

    blob.upload_from_string(existing + new_line, content_type="text/plain")


def write_bq_log(source_file, status, rows_loaded, error_category, error_message, destination_path):
    table_id = f"{PROJECT_ID}.{BQ_DATASET}.{BQ_LOG_TABLE}"
    row = {
        "source_file": source_file,
        "status": status,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "rows_loaded": rows_loaded,
        "error_category": error_category,
        "error_message": error_message,
        "destination_path": destination_path,
    }

    errors = bq_client.insert_rows_json(table_id, [row])
    if errors:
        raise RuntimeError(f"Failed to write ingestion log to BigQuery: {errors}")


def move_blob(bucket_name, source_name, destination_name):
    bucket = storage_client.bucket(bucket_name)
    source_blob = bucket.blob(source_name)

    bucket.copy_blob(source_blob, bucket, destination_name)
    source_blob.delete()


def already_processed(source_file):
    table_id = f"{PROJECT_ID}.{BQ_DATASET}.{BQ_LOG_TABLE}"

    query = f"""
        SELECT COUNT(*) AS count
        FROM `{table_id}`
        WHERE source_file = @source_file
          AND status = 'SUCCESS'
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("source_file", "STRING", source_file)
        ]
    )

    result = list(bq_client.query(query, job_config=job_config).result())
    return result[0]["count"] > 0


def detect_encoding(file_bytes):
    detected = chardet.detect(file_bytes)
    return detected.get("encoding") or "utf-8"


def parse_csv_to_long_rows(file_bytes, source_file):
    encoding = detect_encoding(file_bytes)
    text = file_bytes.decode(encoding, errors="replace")

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        raise ValueError("CSV file is empty")

    headers = rows[0]
    active_columns = [
        index for index, header in enumerate(headers)
        if str(header).strip() != ""
    ]

    if len(headers) < 4:
        raise ValueError("CSV has fewer than 4 columns; cannot apply column D rule")

    output_rows = []
    processed_at = datetime.now(timezone.utc).isoformat()

    for source_row_number, row in enumerate(rows[1:], start=2):
        if len(row) < 4:
            continue

        column_d_value = row[3].strip()
        if column_d_value == "":
            continue

        source_date = row[1].strip() if len(row) > 1 else ""
        source_time = row[2].strip() if len(row) > 2 else ""

        try:
            machine_timestamp = datetime.strptime(
                f"{source_date} {source_time}",
                "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except Exception as exc:
            raise ValueError(
                f"Invalid timestamp at source row {source_row_number}: "
                f"date='{source_date}', time='{source_time}'"
            ) from exc

        for col_index in active_columns:
            if col_index in [1, 2]:
                continue

            if col_index >= len(row):
                continue

            raw_value = row[col_index].strip()
            if raw_value == "":
                continue

            try:
                parameter_value = float(raw_value)
            except Exception as exc:
                raise ValueError(
                    f"Non-numeric value at row {source_row_number}, "
                    f"column {col_index + 1}, header '{headers[col_index]}': '{raw_value}'"
                ) from exc

            output_rows.append({
                "source_file": source_file,
                "processed_at": processed_at,
                "machine_timestamp": machine_timestamp.isoformat(),
                "source_date": source_date,
                "source_time": source_time,
                "source_row_number": source_row_number,
                "parameter_name": headers[col_index].strip(),
                "parameter_value": parameter_value,
            })

    if not output_rows:
        raise ValueError("No valid data rows found after applying column D rule")

    return output_rows


def load_rows_to_bigquery(rows):
    table_id = f"{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"
    dataframe = pd.DataFrame(rows)

    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND
    )

    load_job = bq_client.load_table_from_dataframe(
        dataframe,
        table_id,
        job_config=job_config
    )

    load_job.result()
    return len(dataframe)


def ingest_csv(cloud_event):
    event = cloud_event.data

    bucket_name = event["bucket"]
    file_name = event["name"]

    if not file_name.startswith(WATCH_PREFIX):
        write_bq_log(
            file_name,
            "IGNORED_OUTSIDE_WATCH_FOLDER",
            0,
            "",
            "",
            ""
        )
        return

    if file_name.endswith("/"):
        return

    try:
        write_text_log(bucket_name, f"STARTED | {file_name}")

        if already_processed(file_name):
            destination = f"{FAILED_PREFIX}/duplicate-file/{file_name.split('/')[-1]}"
            move_blob(bucket_name, file_name, destination)

            write_bq_log(
                file_name,
                "DUPLICATE_REJECTED",
                0,
                "duplicate-file",
                "File has already been successfully processed",
                destination
            )
            write_text_log(bucket_name, f"DUPLICATE_REJECTED | {file_name}")
            return

        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(file_name)
        file_bytes = blob.download_as_bytes()

        rows = parse_csv_to_long_rows(file_bytes, file_name)
        rows_loaded = load_rows_to_bigquery(rows)

        destination = f"{PROCESSED_PREFIX}/{file_name.split('/')[-1]}"
        move_blob(bucket_name, file_name, destination)

        write_bq_log(
            file_name,
            "SUCCESS",
            rows_loaded,
            "",
            "",
            destination
        )
        write_text_log(bucket_name, f"SUCCESS | {file_name} | rows_loaded={rows_loaded}")

    except Exception as exc:
        error_message = str(exc)
        traceback_text = traceback.format_exc()

        if "timestamp" in error_message.lower():
            category = "timestamp-error"
        elif "non-numeric" in error_message.lower():
            category = "non-numeric-data"
        elif "empty" in error_message.lower():
            category = "empty-file"
        elif "bigquery" in error_message.lower():
            category = "bigquery-load-error"
        else:
            category = "unknown-error"

        destination = f"{FAILED_PREFIX}/{category}/{file_name.split('/')[-1]}"

        try:
            move_blob(bucket_name, file_name, destination)
        except Exception:
            destination = ""

        write_bq_log(
            file_name,
            "FAILED",
            0,
            category,
            error_message[:1000],
            destination
        )

        write_text_log(
            bucket_name,
            f"FAILED | {file_name} | {category} | {error_message} | {traceback_text[:2000]}"
        )

        raise
