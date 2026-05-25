import os
import tempfile
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.cloud import storage
import google.auth


DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
GCS_BUCKET = os.environ["GCS_BUCKET"]
GCS_WATCH_PREFIX = os.environ["GCS_WATCH_PREFIX"]
SYNC_STATE_PREFIX = os.environ["SYNC_STATE_PREFIX"]

storage_client = storage.Client()


def get_drive_service():
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=credentials)


def already_synced(bucket, drive_file_id):
    marker_blob = bucket.blob(f"{SYNC_STATE_PREFIX}/{drive_file_id}.synced")
    return marker_blob.exists()


def mark_synced(bucket, drive_file_id, file_name):
    marker_blob = bucket.blob(f"{SYNC_STATE_PREFIX}/{drive_file_id}.synced")
    marker_blob.upload_from_string(
        f"{datetime.now(timezone.utc).isoformat()} | {file_name}\n",
        content_type="text/plain",
    )


def sync_drive_to_gcs(request):
    bucket = storage_client.bucket(GCS_BUCKET)
    drive_service = get_drive_service()

    query = (
        f"'{DRIVE_FOLDER_ID}' in parents "
        "and trashed = false "
        "and mimeType != 'application/vnd.google-apps.folder'"
    )

    copied = []
    skipped = []
    files = []

    page_token = None
    while True:
        results = drive_service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token,
        ).execute()

        files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")

        if not page_token:
            break

    for file in files:
        file_id = file["id"]
        file_name = file["name"]

        if not file_name.lower().endswith(".csv"):
            skipped.append(f"{file_name}: not csv")
            continue

        if already_synced(bucket, file_id):
            skipped.append(f"{file_name}: already synced")
            continue

        request_download = drive_service.files().get_media(
            fileId=file_id,
            supportsAllDrives=True,
        )

        with tempfile.NamedTemporaryFile() as tmp:
            downloader = MediaIoBaseDownload(tmp, request_download)

            done = False
            while not done:
                _, done = downloader.next_chunk()

            tmp.seek(0)

            destination_blob = bucket.blob(f"{GCS_WATCH_PREFIX}/{file_name}")
            destination_blob.upload_from_file(tmp, content_type="text/csv")

        mark_synced(bucket, file_id, file_name)
        copied.append(file_name)

    message = {
        "copied": copied,
        "skipped": skipped,
        "copied_count": len(copied),
        "skipped_count": len(skipped),
    }

    return message, 200
