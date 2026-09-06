"""Google Drive API operations for ad_namer."""
from __future__ import annotations
import re
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from auth.credentials import get_service_account_creds

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
]

_LIST_FIELDS = "files(id,name,mimeType,size,videoMediaMetadata,imageMediaMetadata,parents)"


def get_drive_service(_service_account_file: str = ""):
    """Builds a Drive v3 service. Credentials come from env (cloud) or file (local)."""
    creds = get_service_account_creds(DRIVE_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def folder_id_from_url(url_or_id: str) -> str:
    """Extracts folder ID from a Google Drive URL or returns the ID as-is."""
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()


def file_id_from_url(url_or_id: str) -> str:
    """Extracts file ID from a Google Drive URL or returns the ID as-is."""
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()


def list_folder_files(service, folder_id: str, recursive: bool = False) -> list[dict]:
    """
    Returns all non-trashed files in the given folder.
    recursive=True is accepted for API compatibility but ignored
    (use search_files_by_name_prefix for cross-folder searches).
    """
    results = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"

    while True:
        resp = service.files().list(
            q=query,
            fields=_LIST_FIELDS,
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return results


def search_files_by_name_prefix(service, prefix: str) -> list[dict]:
    """
    Searches for all non-trashed, non-folder files whose name starts with prefix.
    Searches across all files accessible to the service account (any folder depth).
    """
    results = []
    page_token = None
    query = (
        f"name contains '{prefix}' and trashed = false "
        "and mimeType != 'application/vnd.google-apps.folder'"
    )

    while True:
        resp = service.files().list(
            q=query,
            fields=_LIST_FIELDS,
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return results


def get_image_aspect_ratio(file_meta: dict) -> Optional[str]:
    """
    Returns aspect ratio tag for static images: '16x9', '4x5', or None if unknown.
    Uses imageMediaMetadata from Drive file metadata.
    """
    img_meta = file_meta.get("imageMediaMetadata", {})
    width = img_meta.get("width")
    height = img_meta.get("height")
    if not width or not height or height == 0:
        return None
    ratio = width / height
    # 16:9 ≈ 1.778 (landscape) — accept 1.6–2.0
    if 1.6 <= ratio <= 2.0:
        return "16x9"
    # 4:5 = 0.8 (portrait) — accept 0.7–0.95
    if 0.7 <= ratio <= 0.95:
        return "4x5"
    # 1:1 square — treat as 4x5 (works in feed)
    if 0.95 < ratio < 1.1:
        return "4x5"
    return None


def get_video_duration_seconds(file_meta: dict) -> Optional[int]:
    """Extracts video duration from Drive file metadata, returns seconds or None."""
    video_meta = file_meta.get("videoMediaMetadata", {})
    millis = video_meta.get("durationMillis")
    if millis:
        return int(int(millis) / 1000)
    return None


def copy_file_to_folder(service, file_id: str, new_name: str, dest_folder_id: str) -> dict:
    """Copies a file to dest_folder with a new name. Original file stays untouched."""
    body = {"name": new_name, "parents": [dest_folder_id]}
    return service.files().copy(fileId=file_id, body=body, fields="id,name,webViewLink").execute()


def move_and_rename_file(service, file_id: str, new_name: str, dest_folder_id: str,
                         source_folder_id: str = "") -> dict:
    """
    Renames a file and moves it to dest_folder by updating its metadata.
    The file stays owned by the original owner — no service account storage needed.
    Requires Editor access on the source file/folder.

    source_folder_id: fallback parent to remove when the Drive API does not return
    the file's parents (happens when the file was shared directly rather than via folder).
    """
    # First get the current parents so we can remove them
    file_meta = service.files().get(
        fileId=file_id, fields="parents", supportsAllDrives=True
    ).execute()
    current_parents = ",".join(file_meta.get("parents", []))

    # Fallback: use the known source folder if the API didn't return parents
    # (avoids "cannotAddParent" / "Increasing the number of parents" error)
    if not current_parents and source_folder_id:
        current_parents = source_folder_id

    def _do_update(remove: str):
        return service.files().update(
            fileId=file_id,
            addParents=dest_folder_id,
            removeParents=remove,
            body={"name": new_name},
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()

    try:
        return _do_update(current_parents)
    except HttpError as e:
        # "Increasing the number of parents is not allowed": removeParents was
        # empty/wrong. Re-fetch the real parents and retry removing all of them.
        if "cannotAddParent" not in str(e) and "Increasing the number" not in str(e):
            raise
        refetched = service.files().get(
            fileId=file_id, fields="parents", supportsAllDrives=True
        ).execute()
        real_parents = ",".join(refetched.get("parents", []))
        if not real_parents:
            raise
        return _do_update(real_parents)


def rename_file(service, file_id: str, new_name: str) -> dict:
    """Renames a file in place (no move). Requires Editor access on the file."""
    return service.files().update(
        fileId=file_id,
        body={"name": new_name},
        fields="id,name,webViewLink",
    ).execute()


def trash_file(service, file_id: str) -> dict:
    """Moves a file to the Drive trash (recoverable for 30 days)."""
    return service.files().update(
        fileId=file_id,
        body={"trashed": True},
        fields="id,name,trashed",
    ).execute()


def upload_file_to_folder(service, local_path: str, new_name: str, dest_folder_id: str) -> dict:
    """Uploads a local file to dest_folder with new_name. Returns file metadata."""
    from googleapiclient.http import MediaFileUpload
    import mimetypes
    mime_type, _ = mimetypes.guess_type(local_path)
    if not mime_type:
        mime_type = "application/octet-stream"
    body = {"name": new_name, "parents": [dest_folder_id]}
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
    return service.files().create(
        body=body, media_body=media, fields="id,name,webViewLink"
    ).execute()


def make_drive_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view?usp=drive_link"


def is_video(mime_type: str) -> bool:
    return mime_type.startswith("video/")


def is_image(mime_type: str) -> bool:
    return mime_type.startswith("image/")


def get_file_extension(name: str) -> str:
    parts = name.rsplit(".", 1)
    return f".{parts[1].lower()}" if len(parts) == 2 else ""
