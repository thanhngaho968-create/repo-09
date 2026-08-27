"""
runners/gsheet_helper.py - Direct Google Sheets Queue Updater for Cloud Runners
Updates task status, progress, title, and Google Drive links in the Media_Queue tab.
"""
import os
import json
import base64
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [GSheetHelper] %(message)s")

SPREADSHEET_ID = os.environ.get("GSHEET_QUEUE_ID", "1qCYT9HV99mTkwyjL4EemqLeIGg_rZpKw0wH0yLl1IEQ")
TAB_MEDIA = "Media_Queue"

HEADERS = ["ID", "Thời Gian", "Lệnh", "Tiêu Đề", "URL", "Trạng Thái", "Tiến Độ", "Drive Link", "Chat ID", "Thread ID"]


def get_gspread_client():
    """Initializes gspread client from Service Account or User OAuth2 base64 secret."""
    import gspread
    from google.oauth2 import service_account

    sa_b64 = os.environ.get("GDRIVE_SA_BASE64", "").strip()
    if sa_b64:
        try:
            missing_padding = len(sa_b64) % 4
            if missing_padding:
                sa_b64 += '=' * (4 - missing_padding)
            sa_info = json.loads(base64.b64decode(sa_b64).decode("utf-8"))
            scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
            return gspread.authorize(creds)
        except Exception as e:
            logger.warning(f"GSheet SA initialization error: {e}")

    # Fallback to local service account
    for p in ["service_account.json", "/media/vpsg16gb/HaRiDisk/Telegram_Command_Center/service_account.json"]:
        if os.path.exists(p):
            try:
                scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
                creds = service_account.Credentials.from_service_account_file(p, scopes=scopes)
                return gspread.authorize(creds)
            except Exception:
                pass

    return None


def update_media_task_status(
    row_index: int,
    status: Optional[str] = None,
    progress: Optional[str] = None,
    title: Optional[str] = None,
    drive_link: Optional[str] = None
) -> bool:
    """
    Updates a task's cells in the Media_Queue tab:
    - Col D (4): Tiêu Đề
    - Col F (6): Trạng Thái
    - Col G (7): Tiến Độ
    - Col H (8): Drive Link
    """
    if not row_index or row_index < 2:
        logger.warning(f"Invalid row_index: {row_index}")
        return False

    try:
        gc = get_gspread_client()
        if not gc:
            logger.warning("Google Sheets client unavailable, skipping sheet update.")
            return False

        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = sh.worksheet(TAB_MEDIA)

        updates = []
        if title is not None:
            updates.append({"range": f"D{row_index}", "values": [[str(title)]]})
        if status is not None:
            updates.append({"range": f"F{row_index}", "values": [[str(status)]]})
        if progress is not None:
            updates.append({"range": f"G{row_index}", "values": [[str(progress)]]})
        if drive_link is not None:
            updates.append({"range": f"H{row_index}", "values": [[str(drive_link)]]})

        if updates:
            ws.batch_update(updates)
            logger.info(f"📊 Updated GSheet row #{row_index}: status='{status}', progress='{progress}'")
            return True
    except Exception as e:
        logger.warning(f"⚠️ GSheet update warning on row #{row_index}: {e}")

    return False
