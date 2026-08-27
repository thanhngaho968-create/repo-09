"""
runners/telegram_helper.py - Telegram Bot API Helper for Cloud Runners
Features:
- Dual-path routing: Cloudflare Edge Relay (Primary) + Direct Telegram API (Fallback).
- Hardcoded enforcement of @youtube2drive_Bot (8798886722).
- Zero NWL bot token cross-contamination barrier.
- Rich media upload: sendMessage, editMessageText, sendPhoto, sendVideo, sendMediaGroup, sendDocument.
"""
import os
import time
import requests
import json
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [TelegramHelper] %(message)s")

DEFAULT_CC_TOKEN = "8798886722:AAFLRQwdonCZIJXuvGm9NbvyyVlETxsWjYw"
NWL_FORBIDDEN_PREFIX = "8944836049"

CF_RELAY_URL = os.environ.get("CF_RELAY_URL", "https://telegram-command-edge.hothihuong113.workers.dev").strip()
CF_RELAY_SECRET = os.environ.get("CF_RELAY_SECRET", "HaRiSecret_2026_SecureRelay").strip()

RAW_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or DEFAULT_CC_TOKEN
if RAW_BOT_TOKEN.startswith(NWL_FORBIDDEN_PREFIX):
    logger.warning("🚨 [ANTI CROSS-CONTAMINATION] Blocked NWL token in TelegramHelper! Enforcing @youtube2drive_Bot.")
    BOT_TOKEN = DEFAULT_CC_TOKEN
else:
    BOT_TOKEN = RAW_BOT_TOKEN


def make_tg_request(method: str, data: Optional[Dict[str, Any]] = None, files: Optional[Dict[str, Any]] = None, max_retries: int = 5, timeout: int = 120) -> Dict[str, Any]:
    """Executes Telegram API request via Cloudflare Relay with Direct API fallback."""
    for attempt in range(1, max_retries + 1):
        # 1. Tier 1: Cloudflare Edge Relay
        if CF_RELAY_URL:
            base_relay = CF_RELAY_URL.rstrip("/")
            url = f"{base_relay}/relay/{method}"
            headers = {"X-Relay-Secret": CF_RELAY_SECRET} if CF_RELAY_SECRET else {}
            try:
                res = requests.post(url, headers=headers, data=data, files=files, timeout=timeout)
                try:
                    res_json = res.json()
                    if res_json.get("ok"):
                        return res_json
                    if res_json.get("error_code") == 429:
                        retry_after = res_json.get("parameters", {}).get("retry_after", 5)
                        logger.warning(f"Telegram 429 Rate Limit. Sleeping {retry_after + 2}s (Attempt {attempt}/{max_retries})...")
                        time.sleep(retry_after + 2)
                        continue
                    if res_json.get("error_code") == 400:
                        return res_json
                    logger.warning(f"[Relay Attempt {attempt}/{max_retries}] API Error: {res_json}")
                except Exception:
                    if res.status_code == 200:
                        return {"ok": True}
            except Exception as e:
                logger.warning(f"[Relay Attempt {attempt}/{max_retries}] Connection Error: {e}")

        # 2. Tier 2: Direct Telegram API Fallback
        if BOT_TOKEN:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
            try:
                res = requests.post(url, data=data, files=files, timeout=timeout)
                try:
                    res_json = res.json()
                    if res_json.get("ok"):
                        return res_json
                    if res_json.get("error_code") == 429:
                        retry_after = res_json.get("parameters", {}).get("retry_after", 5)
                        logger.warning(f"Direct TG 429 Rate Limit. Sleeping {retry_after + 2}s (Attempt {attempt}/{max_retries})...")
                        time.sleep(retry_after + 2)
                        continue
                    if res_json.get("error_code") == 400:
                        return res_json
                    logger.warning(f"[Direct TG Attempt {attempt}/{max_retries}] API Error: {res_json}")
                except Exception:
                    if res.status_code == 200:
                        return {"ok": True}
            except Exception as e:
                logger.warning(f"[Direct TG Attempt {attempt}/{max_retries}] Connection Error: {e}")

        if attempt < max_retries:
            time.sleep(attempt * 2)

    return {"ok": False, "error": f"Failed {method} after {max_retries} attempts"}


def send_message(chat_id: str, text: str, parse_mode: str = "HTML", reply_to_message_id: Optional[int] = None, thread_id: Optional[int] = None) -> Dict[str, Any]:
    """Sends a text message to Telegram chat or forum thread."""
    data = {"chat_id": str(chat_id), "text": text, "parse_mode": parse_mode}
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    if thread_id:
        data["message_thread_id"] = thread_id

    res = make_tg_request("sendMessage", data=data)
    if not res.get("ok") and res.get("error_code") == 400 and parse_mode:
        data.pop("parse_mode", None)
        return make_tg_request("sendMessage", data=data)
    return res


def edit_message(chat_id: str, message_id: int, text: str, parse_mode: str = "HTML") -> Dict[str, Any]:
    """Edits an existing Telegram message with HTML fallback."""
    data = {"chat_id": str(chat_id), "message_id": message_id, "text": text, "parse_mode": parse_mode}
    res = make_tg_request("editMessageText", data=data)
    if not res.get("ok") and res.get("error_code") == 400 and parse_mode:
        data.pop("parse_mode", None)
        return make_tg_request("editMessageText", data=data)
    return res


def send_photo(chat_id: str, photo_path_or_url: str, caption: str = "", reply_to_message_id: Optional[int] = None, thread_id: Optional[int] = None) -> Dict[str, Any]:
    """Sends a photo via URL or file upload."""
    data = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    if thread_id:
        data["message_thread_id"] = thread_id

    if isinstance(photo_path_or_url, str) and (photo_path_or_url.startswith("http://") or photo_path_or_url.startswith("https://")):
        data["photo"] = photo_path_or_url
        res = make_tg_request("sendPhoto", data=data)
        if res.get("ok"):
            return res
        try:
            r_img = requests.get(photo_path_or_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            if r_img.status_code == 200:
                files = {"photo": ("cover.jpg", r_img.content, "image/jpeg")}
                data_clean = {k: v for k, v in data.items() if k != "photo"}
                return make_tg_request("sendPhoto", data=data_clean, files=files)
        except Exception:
            pass
        return res
    elif os.path.exists(photo_path_or_url):
        with open(photo_path_or_url, "rb") as f:
            files = {"photo": (os.path.basename(photo_path_or_url), f, "image/jpeg")}
            return make_tg_request("sendPhoto", data=data, files=files)
    return {"ok": False, "error": "Invalid photo path or URL"}


def send_video(
    chat_id: str,
    video_path: str,
    caption: str = "",
    thumb_path: Optional[str] = None,
    duration: int = 0,
    width: int = 0,
    height: int = 0,
    reply_to_message_id: Optional[int] = None,
    thread_id: Optional[int] = None,
    supports_streaming: bool = True
) -> Dict[str, Any]:
    """Uploads and streams a video to Telegram."""
    if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
        return {"ok": False, "error": f"Video missing or empty: {video_path}"}

    data = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": "HTML",
        "supports_streaming": "true" if supports_streaming else "false"
    }
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    if thread_id:
        data["message_thread_id"] = thread_id
    if duration > 0:
        data["duration"] = int(duration)
    if width > 0 and height > 0:
        data["width"] = int(width)
        data["height"] = int(height)

    opened_files = []
    try:
        vf = open(video_path, "rb")
        opened_files.append(vf)
        files = {"video": (os.path.basename(video_path), vf, "video/mp4")}
        if thumb_path and os.path.exists(thumb_path):
            tf = open(thumb_path, "rb")
            opened_files.append(tf)
            files["thumbnail"] = (os.path.basename(thumb_path), tf, "image/jpeg")

        return make_tg_request("sendVideo", data=data, files=files, timeout=300)
    finally:
        for f in opened_files:
            try:
                f.close()
            except Exception:
                pass


def send_media_group(chat_id: str, media_paths: List[str], caption: str = "", reply_to_message_id: Optional[int] = None, thread_id: Optional[int] = None) -> Dict[str, Any]:
    """Sends a grouped album of photos or videos."""
    if not media_paths:
        return {"ok": False, "error": "No media paths provided"}

    media_list = []
    files = {}
    opened_files = []
    try:
        for idx, path in enumerate(media_paths[:10]):
            ext = os.path.splitext(path)[1].lower()
            file_key = f"file_{idx}"
            f = open(path, "rb")
            opened_files.append(f)
            files[file_key] = f

            m_type = "video" if ext in [".mp4", ".mkv", ".mov"] else "photo"
            item = {"type": m_type, "media": f"attach://{file_key}"}
            if idx == 0 and caption:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media_list.append(item)

        data = {"chat_id": str(chat_id), "media": json.dumps(media_list)}
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        if thread_id:
            data["message_thread_id"] = thread_id

        return make_tg_request("sendMediaGroup", data=data, files=files, timeout=300)
    finally:
        for f in opened_files:
            try:
                f.close()
            except Exception:
                pass


def send_document(chat_id: str, document_path: str, caption: str = "", reply_to_message_id: Optional[int] = None, thread_id: Optional[int] = None) -> Dict[str, Any]:
    """Uploads a generic document or audio file to Telegram."""
    if not os.path.exists(document_path) or os.path.getsize(document_path) == 0:
        return {"ok": False, "error": f"Document missing or empty: {document_path}"}

    data = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    if thread_id:
        data["message_thread_id"] = thread_id

    with open(document_path, "rb") as df:
        files = {"document": (os.path.basename(document_path), df, "application/octet-stream")}
        return make_tg_request("sendDocument", data=data, files=files, timeout=300)
