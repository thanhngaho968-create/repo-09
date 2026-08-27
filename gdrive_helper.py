"""
runners/gdrive_helper.py - Google Drive v3 Integration for Cloud Runners
Supports 5TB User OAuth2 (Primary), Service Account (Fallback), Folder Hierarchies,
Deduplication Shields, and Resumable Multi-Chunk Uploads.
"""
import os
import io
import json
import base64
import time
import random
import logging
from typing import Optional, Tuple, Dict, Any, List
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [GDriveHelper] %(message)s")

DEFAULT_OWNER_EMAIL = "hothihuong113@gmail.com"
DEFAULT_DRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "1kQGnr2q4rXJ3hUKZvocFLMdpsoDBp2m4")
SCOPES = ["https://www.googleapis.com/auth/drive"]

_drive_service = None


def retry_on_429(func, *args, max_retries=5, backoff_factor=2, **kwargs):
    """Executes a function with exponential backoff on rate limits or transient errors."""
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            err_msg = str(e)
            is_transient = False
            if any(k in err_msg.lower() for k in ["429", "quota", "limit", "ratelimit", "userate", "backenderror"]):
                is_transient = True
            if hasattr(e, 'resp') and getattr(e.resp, 'status', None) in [429, 500, 502, 503, 504]:
                is_transient = True
            elif hasattr(e, 'response') and getattr(e.response, 'status_code', None) in [429, 500, 502, 503, 504]:
                is_transient = True

            if is_transient and attempt < max_retries - 1:
                sleep_time = (backoff_factor ** attempt) + random.uniform(1.0, 3.0)
                logger.warning(
                    f"⚠️ Google API transient/quota error: {err_msg[:120]}. "
                    f"Retrying in {sleep_time:.2f}s (Attempt {attempt + 1}/{max_retries})..."
                )
                time.sleep(sleep_time)
            else:
                raise


def get_drive_service():
    """Initializes and returns authenticated Google Drive v3 service."""
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    # 1. Primary: User OAuth2 (Direct 5TB Quota)
    oauth_info = None
    oauth_b64 = os.environ.get("GDRIVE_OAUTH_BASE64", "").strip()
    if oauth_b64:
        try:
            missing_padding = len(oauth_b64) % 4
            if missing_padding:
                oauth_b64 += '=' * (4 - missing_padding)
            oauth_info = json.loads(base64.b64decode(oauth_b64).decode("utf-8"))
        except Exception as e:
            logger.warning(f"Failed to decode GDRIVE_OAUTH_BASE64: {e}")

    if not oauth_info:
        for path in ["user_oauth2.json", "/media/vpsg16gb/HaRiDisk/Telegram_Command_Center/user_oauth2.json"]:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        oauth_info = json.load(f)
                    break
                except Exception:
                    pass

    if oauth_info and oauth_info.get("refresh_token"):
        try:
            from google.oauth2.credentials import Credentials
            import google.auth.transport.requests
            logger.info("🔑 Initializing Google Drive User OAuth2 (5TB Direct Storage)...")
            creds = Credentials(
                None,
                refresh_token=oauth_info["refresh_token"],
                token_uri=oauth_info.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=oauth_info["client_id"],
                client_secret=oauth_info["client_secret"],
                scopes=oauth_info.get("scopes", SCOPES)
            )
            req = google.auth.transport.requests.Request()
            creds.refresh(req)
            _drive_service = build("drive", "v3", credentials=creds)
            logger.info("✅ Google Drive User OAuth2 authenticated successfully!")
            return _drive_service
        except Exception as oe:
            logger.warning(f"⚠️ User OAuth2 refresh failed ({oe}), falling back to Service Account...")

    # 2. Secondary Fallback: Service Account
    sa_info = None
    sa_b64 = os.environ.get("GDRIVE_SA_BASE64", "").strip()
    if sa_b64:
        try:
            missing_padding = len(sa_b64) % 4
            if missing_padding:
                sa_b64 += '=' * (4 - missing_padding)
            sa_info = json.loads(base64.b64decode(sa_b64).decode("utf-8"))
        except Exception as e:
            logger.warning(f"Failed to decode GDRIVE_SA_BASE64: {e}")

    if not sa_info:
        sa_raw = os.environ.get("GDRIVE_SA_JSON", "").strip()
        if sa_raw:
            try:
                sa_info = json.loads(sa_raw)
            except Exception:
                pass

    if not sa_info:
        for path in ["service_account.json", "/media/vpsg16gb/HaRiDisk/Telegram_Command_Center/service_account.json"]:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        sa_info = json.load(f)
                    break
                except Exception:
                    pass

    if not sa_info:
        raise ValueError("Missing Google Drive credentials (GDRIVE_OAUTH_BASE64 or GDRIVE_SA_BASE64 required)")

    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    _drive_service = build("drive", "v3", credentials=creds)
    logger.info("✅ Google Drive Service Account authenticated successfully!")
    return _drive_service


def find_drive_folder(folder_name: str, parent_id: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """Finds an existing non-trashed folder by name and parent_id."""
    def _run():
        service = get_drive_service()
        safe_name = folder_name.replace("'", "\\'")
        query = f"mimeType='application/vnd.google-apps.folder' and name='{safe_name}' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        res = service.files().list(
            q=query,
            fields="files(id, name, webViewLink)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        files = res.get("files", [])
        if files:
            f_id = files[0]["id"]
            f_link = files[0].get("webViewLink") or f"https://drive.google.com/drive/folders/{f_id}"
            return f_id, f_link
        return None

    return retry_on_429(_run)


def create_drive_folder(folder_name: str, parent_id: Optional[str] = None, owner_email: str = DEFAULT_OWNER_EMAIL) -> Tuple[str, str]:
    """Creates a new folder or returns existing folder if already present."""
    existing = find_drive_folder(folder_name, parent_id)
    if existing:
        logger.info(f"📁 Reusing existing Google Drive folder '{folder_name}' (ID: {existing[0]})")
        return existing

    def _run():
        service = get_drive_service()
        meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder"
        }
        if parent_id:
            meta["parents"] = [parent_id]

        folder = service.files().create(
            body=meta,
            fields="id, name, webViewLink",
            supportsAllDrives=True
        ).execute()

        folder_id = folder.get("id")
        folder_link = folder.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder_id}"
        logger.info(f"📁 Created new Google Drive folder '{folder_name}' (ID: {folder_id})")

        target_email = owner_email or DEFAULT_OWNER_EMAIL
        if target_email:
            try:
                service.permissions().create(
                    fileId=folder_id,
                    body={"type": "user", "role": "writer", "emailAddress": target_email},
                    sendNotificationEmail=False,
                    supportsAllDrives=True
                ).execute()
            except Exception as pe:
                logger.warning(f"Permission grant warning for folder '{folder_name}': {pe}")

        return folder_id, folder_link

    return retry_on_429(_run)


def upload_file_to_drive(
    local_path: str,
    file_name: str,
    parent_folder_id: Optional[str] = None,
    mime_type: Optional[str] = None,
    owner_email: str = DEFAULT_OWNER_EMAIL
) -> str:
    """
    Uploads a local file to Google Drive with resumable 10MB chunks.
    Performs strict deduplication check before uploading.
    Returns direct webViewLink.
    """
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Local file not found: {local_path}")

    target_folder = parent_folder_id or DEFAULT_DRIVE_FOLDER_ID
    service = get_drive_service()

    if not mime_type:
        ext = os.path.splitext(file_name)[1].lower()
        mime_map = {
            ".mp4": "video/mp4",
            ".mkv": "video/x-matroska",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp"
        }
        mime_type = mime_map.get(ext, "application/octet-stream")

    def _run():
        nonlocal service
        # DEDUPLICATION CHECK
        escaped_name = file_name.replace("'", "\\'")
        q = f"'{target_folder}' in parents and name = '{escaped_name}' and trashed = false"
        try:
            res = service.files().list(
                q=q,
                fields="files(id, name, webViewLink, size, trashed)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()
        except Exception:
            global _drive_service
            _drive_service = None
            service = get_drive_service()
            res = service.files().list(
                q=q,
                fields="files(id, name, webViewLink, size, trashed)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()

        existing_files = res.get("files", [])
        if existing_files:
            valid_files = [f for f in existing_files if int(f.get("size", 0)) > 1024 * 50]
            if valid_files:
                primary = valid_files[0]
                logger.info(f"⚡ [DEDUPLICATION SHIELD] File '{file_name}' already exists on GDrive (ID: {primary['id']}). Skipping upload!")
                return primary.get("webViewLink") or f"https://drive.google.com/file/d/{primary['id']}/view"

        meta = {"name": file_name}
        if target_folder:
            meta["parents"] = [target_folder]

        media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True, chunksize=10 * 1024 * 1024)
        file_obj = service.files().create(
            body=meta,
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True
        ).execute()

        file_id = file_obj["id"]
        logger.info(f"✅ Uploaded '{file_name}' to GDrive (ID: {file_id})")

        target_email = owner_email or DEFAULT_OWNER_EMAIL
        if target_email:
            try:
                service.permissions().create(
                    fileId=file_id,
                    body={"type": "user", "role": "writer", "emailAddress": target_email},
                    sendNotificationEmail=False,
                    supportsAllDrives=True
                ).execute()
            except Exception as pe:
                logger.warning(f"Permission grant warning for '{file_name}': {pe}")

        return file_obj.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"

    return retry_on_429(_run)
