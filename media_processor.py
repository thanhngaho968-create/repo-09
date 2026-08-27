"""
runners/media_processor.py - Unified Media Processing Engine for Cloud Runners
Executes:
- /wf3: YouTube Single Video / Shorts (MP4 + MP3)
- /wf2: YouTube Playlist (Dedicated GDrive Subfolder, Resilient Error Tolerance, Multi-video Progress)
- /wf1: YouTube Channel (Playlists & Videos Hierarchy)
- /wf4: TikTok Video (No-watermark MP4 + MP3)
- /wf6: Facebook / Instagram (Reels, Videos, Carousels, Stories)
- Google Drive 5TB Direct Upload with Deduplication Shield
- Realtime Telegram & Google Sheets Per-Step Reporting
- Strict Zero-VPS Fallback & Fast-Fail Auth Flagging
"""
import os
import sys
import re
import json
import time
import html
import shutil
import base64
import subprocess
import logging
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse
import requests

try:
    from runners import gdrive_helper, telegram_helper, gsheet_helper
except ImportError:
    try:
        import gdrive_helper
        import telegram_helper
        import gsheet_helper
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gdrive_helper
        import telegram_helper
        import gsheet_helper

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [MediaProcessor] %(message)s")

DEFAULT_DRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "1kQGnr2q4rXJ3hUKZvocFLMdpsoDBp2m4")
FB_IG_FOLDER_ID = "1uWtFeNQcXeOEFIn8zGO0c6rBRZrJg__r"
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "hothihuong113@gmail.com")

RUNNER_REPO = os.environ.get("RUNNER_REPO") or os.environ.get("GITHUB_REPOSITORY") or "Cloud Runner"


def clean_filename(name: str) -> str:
    """Sanitizes strings for safe filenames."""
    if not name:
        return "media_file"
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return cleaned[:100] if len(cleaned) > 100 else cleaned


def format_bytes(size: int) -> str:
    """Formats raw bytes into human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def get_ytdlp_cmd() -> List[str]:
    """Constructs robust yt-dlp command line options."""
    ytdlp_bin = os.path.expanduser("~/.local/bin/yt-dlp")
    if not os.path.exists(ytdlp_bin):
        ytdlp_bin = "yt-dlp"

    base = [
        ytdlp_bin,
        "--no-check-certificates",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "--extractor-args", "youtube:player_client=android,web,mweb",
        "--retries", "3",
        "--fragment-retries", "5",
        "--no-warnings"
    ]
    if os.path.exists("cookies.txt") and os.path.getsize("cookies.txt") > 0:
        base.extend(["--cookies", "cookies.txt"])
    return base


def parse_task_payload() -> Dict[str, Any]:
    """Parses task payload from environment variables or JSON."""
    raw_payload = os.environ.get("TASK_PAYLOAD", "").strip()
    payload = {}
    if raw_payload:
        try:
            payload = json.loads(raw_payload)
        except Exception:
            try:
                b64_data = raw_payload
                missing_padding = len(b64_data) % 4
                if missing_padding:
                    b64_data += '=' * (4 - missing_padding)
                decoded = base64.b64decode(b64_data).decode("utf-8")
                payload = json.loads(decoded)
            except Exception as e:
                logger.warning(f"Could not parse TASK_PAYLOAD: {e}")

    task_id = payload.get("task_id") or os.environ.get("TASK_ID", f"task_media_{int(time.time())}")
    cmd = payload.get("cmd") or os.environ.get("CMD", "/wf3")
    media_type = payload.get("media_type") or os.environ.get("MEDIA_TYPE", "single")
    url = payload.get("url") or os.environ.get("URL", "")
    title = payload.get("title") or os.environ.get("TITLE", "")
    chat_id = payload.get("chat_id") or os.environ.get("CHAT_ID", "")
    thread_id = payload.get("thread_id") or os.environ.get("THREAD_ID")
    status_msg_id = payload.get("status_msg_id") or os.environ.get("STATUS_MSG_ID")
    sheet_row = payload.get("sheet_row") or os.environ.get("SHEET_ROW")
    drive_folder_id = payload.get("drive_folder_id") or os.environ.get("DRIVE_FOLDER_ID") or DEFAULT_DRIVE_FOLDER_ID
    fmt = payload.get("format") or os.environ.get("FORMAT", "mp4+mp3")

    return {
        "task_id": task_id,
        "cmd": str(cmd).strip().lower(),
        "media_type": str(media_type).strip().lower(),
        "url": str(url).strip(),
        "title": str(title).strip(),
        "chat_id": str(chat_id).strip() if chat_id else "",
        "thread_id": int(thread_id) if thread_id else None,
        "status_msg_id": int(status_msg_id) if status_msg_id else None,
        "sheet_row": int(sheet_row) if sheet_row else None,
        "drive_folder_id": str(drive_folder_id).strip(),
        "format": str(fmt).strip()
    }


def check_for_auth_block(error_str: str) -> Optional[str]:
    """Detects authentication, login, or bot-detection barriers."""
    err_lower = (error_str or "").lower()
    if any(k in err_lower for k in ["sign in to confirm your age", "confirm your age", "age-restricted", "age restricted"]):
        return "Need Auth/Cookie (Age-Restricted Video)"
    if any(k in err_lower for k in ["members-only", "join this channel", "members only"]):
        return "Need Auth/Cookie (Members-Only Video)"
    if any(k in err_lower for k in ["private video", "this video is private", "login required", "requires authentication"]):
        return "Need Auth/Cookie (Private Video)"
    if any(k in err_lower for k in ["bot detection", "sign in to confirm you're not a bot", "captcha"]):
        return "Need Auth/Cookie (Bot Detection Block)"
    if "http error 403" in err_lower or "403 forbidden" in err_lower:
        return "Need Auth/Cookie (HTTP 403 Forbidden)"
    return None


def get_media_info(url: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Inspects media info JSON without downloading."""
    cmd = get_ytdlp_cmd() + ["--dump-json", "--flat-playlist", url]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        auth_err = check_for_auth_block(proc.stderr)
        if auth_err:
            return None, auth_err
        return None, proc.stderr.strip() or "Unknown error fetching media info"

    lines = [l.strip() for l in proc.stdout.strip().split("\n") if l.strip()]
    if not lines:
        return None, "Empty metadata response"

    try:
        if len(lines) == 1:
            return json.loads(lines[0]), None
        else:
            return {"entries": [json.loads(l) for l in lines]}, None
    except Exception as e:
        return None, str(e)


# ==============================================================================
# TASK HANDLERS
# ==============================================================================

def handle_single_video(task: Dict[str, Any], temp_dir: str) -> bool:
    """Handles /wf3 single video or short download (MP4 + MP3)."""
    url = task["url"]
    chat_id = task["chat_id"]
    thread_id = task["thread_id"]
    status_msg_id = task["status_msg_id"]
    sheet_row = task["sheet_row"]
    folder_id = task["drive_folder_id"]

    logger.info(f"🎬 Processing single video: {url}")
    if chat_id and status_msg_id:
        telegram_helper.edit_message(
            chat_id, status_msg_id,
            f"⚡ <b>[Cloud Runner: {RUNNER_REPO}] Đang phân tích video...</b>\n🔗 <code>{html.escape(url)}</code>"
        )

    info, err = get_media_info(url)
    if err:
        auth_block = check_for_auth_block(err)
        status_label = auth_block or f"Error ({err[:40]})"
        logger.error(f"❌ Failed to fetch info: {err}")
        if sheet_row:
            gsheet_helper.update_media_task_status(sheet_row, status=status_label, progress="Lỗi trích xuất thông tin")
        if chat_id and status_msg_id:
            telegram_helper.edit_message(
                chat_id, status_msg_id,
                f"❌ <b>LỖI TẢI VIDEO ({RUNNER_REPO}):</b>\n<code>{html.escape(err[:200])}</code>\n"
                f"Trạng thái: <code>{status_label}</code>"
            )
        return False

    title = clean_filename(info.get("title") or task["title"] or f"Video_{int(time.time())}")
    uploader = info.get("uploader") or info.get("channel") or ""
    duration_s = info.get("duration") or 0

    if sheet_row:
        gsheet_helper.update_media_task_status(sheet_row, title=title, progress="10% (Đang tải MP4 & MP3)")

    if chat_id and status_msg_id:
        telegram_helper.edit_message(
            chat_id, status_msg_id,
            f"📥 <b>[Cloud Runner: {RUNNER_REPO}] Đang tải video & xuất MP3:</b>\n"
            f"🎬 <b>Tiêu đề:</b> <code>{html.escape(title)}</code>\n"
            f"📊 <b>Tiến độ:</b> <code>10% (Tải MP4 & MP3...)</code>"
        )

    v_path = os.path.join(temp_dir, f"{title}.mp4")
    a_path = os.path.join(temp_dir, f"{title}.mp3")

    # 1. Download MP4
    cmd_v = get_ytdlp_cmd() + [
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "-o", v_path,
        url
    ]
    proc_v = subprocess.run(cmd_v, capture_output=True, text=True)
    if not os.path.exists(v_path) or os.path.getsize(v_path) == 0:
        err_v = proc_v.stderr or "yt-dlp failed to download MP4"
        auth_block = check_for_auth_block(err_v)
        status_label = auth_block or "Error (Download MP4 Failed)"
        if sheet_row:
            gsheet_helper.update_media_task_status(sheet_row, status=status_label, progress=err_v[:50])
        if chat_id and status_msg_id:
            telegram_helper.edit_message(
                chat_id, status_msg_id,
                f"❌ <b>Lỗi tải MP4:</b> <code>{html.escape(err_v[:150])}</code>"
            )
        return False

    # 2. Extract Audio MP3
    cmd_a = get_ytdlp_cmd() + [
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "-o", a_path,
        url
    ]
    subprocess.run(cmd_a, capture_output=True)

    if chat_id and status_msg_id:
        telegram_helper.edit_message(
            chat_id, status_msg_id,
            f"☁️ <b>[Cloud Runner: {RUNNER_REPO}] Đang tải lên Google Drive (5TB OAuth2)...</b>\n"
            f"🎬 <b>Tiêu đề:</b> <code>{html.escape(title)}</code>\n"
            f"📊 <b>Tiến độ:</b> <code>70% (Đang upload...)</code>"
        )

    # 3. Upload to Google Drive with Deduplication Shield
    v_link = gdrive_helper.upload_file_to_drive(v_path, f"{title}.mp4", folder_id, mime_type="video/mp4", owner_email=OWNER_EMAIL)
    a_link = None
    if os.path.exists(a_path) and os.path.getsize(a_path) > 1024:
        a_link = gdrive_helper.upload_file_to_drive(a_path, f"{title}.mp3", folder_id, mime_type="audio/mpeg", owner_email=OWNER_EMAIL)

    primary_link = v_link or a_link or f"https://drive.google.com/drive/folders/{folder_id}"

    # 4. Final Updates
    if sheet_row:
        gsheet_helper.update_media_task_status(sheet_row, status="Completed", progress="100%", title=title, drive_link=primary_link)

    if chat_id:
        final_msg = (
            f"🎉 <b>ĐÃ HOÀN THÀNH TẢI VIDEO!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎬 <b>Tiêu đề:</b> <code>{html.escape(title)}</code>\n"
            f"👤 <b>Kênh:</b> <code>{html.escape(uploader)}</code>\n"
            f"⚙️ <b>Thực thi bởi:</b> <code>{html.escape(RUNNER_REPO)}</code>\n"
            f"📁 <b>Google Drive MP4:</b> <a href=\"{v_link}\">Mở Video MP4</a>\n"
        )
        if a_link:
            final_msg += f"🎵 <b>Google Drive MP3:</b> <a href=\"{a_link}\">Mở Audio MP3</a>\n"

        if status_msg_id:
            telegram_helper.edit_message(chat_id, status_msg_id, final_msg)
        else:
            telegram_helper.send_message(chat_id, final_msg, thread_id=thread_id)

    logger.info(f"✅ Completed single video task: {title}")
    return True


def handle_playlist(task: Dict[str, Any], temp_dir: str) -> bool:
    """
    Handles /wf2 YouTube Playlist with resilient error tolerance:
    - Creates dedicated Google Drive subfolder.
    - Iterates through all videos; skips broken/copyrighted videos without failing entire job.
    - Realtime progress updates per video.
    """
    url = task["url"]
    chat_id = task["chat_id"]
    thread_id = task["thread_id"]
    status_msg_id = task["status_msg_id"]
    sheet_row = task["sheet_row"]
    parent_folder_id = task["drive_folder_id"]

    logger.info(f"📋 Processing playlist: {url}")
    if chat_id and status_msg_id:
        telegram_helper.edit_message(
            chat_id, status_msg_id,
            f"⚡ <b>[Cloud Runner: {RUNNER_REPO}] Đang quét danh sách phát...</b>\n🔗 <code>{html.escape(url)}</code>"
        )

    # 1. Fetch playlist metadata
    cmd = get_ytdlp_cmd() + ["--flat-playlist", "-J", url]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = proc.stderr.strip()
        auth_block = check_for_auth_block(err)
        status_label = auth_block or f"Error (Playlist Scan Failed)"
        if sheet_row:
            gsheet_helper.update_media_task_status(sheet_row, status=status_label, progress=err[:40])
        if chat_id and status_msg_id:
            telegram_helper.edit_message(chat_id, status_msg_id, f"❌ <b>Lỗi quét Playlist:</b> <code>{html.escape(err[:150])}</code>")
        return False

    try:
        pl_data = json.loads(proc.stdout)
    except Exception as e:
        logger.error(f"Failed to parse playlist JSON: {e}")
        return False

    pl_title = clean_filename(pl_data.get("title") or "YouTube_Playlist")
    entries = pl_data.get("entries", [])
    total_count = len(entries)

    if total_count == 0:
        if sheet_row:
            gsheet_helper.update_media_task_status(sheet_row, status="Completed", progress="0/0 (Playlist rỗng)", title=pl_title)
        if chat_id and status_msg_id:
            telegram_helper.edit_message(chat_id, status_msg_id, f"⚠️ <b>Playlist rỗng hoặc không có video công khai.</b>")
        return True

    # 2. Create dedicated subfolder on Google Drive
    subfolder_name = f"Playlist - {pl_title}"
    subfolder_id, subfolder_link = gdrive_helper.create_drive_folder(subfolder_name, parent_folder_id, owner_email=OWNER_EMAIL)

    if sheet_row:
        gsheet_helper.update_media_task_status(
            sheet_row,
            title=pl_title,
            status=f"In Progress ({RUNNER_REPO})",
            progress=f"[0/{total_count}] 0%",
            drive_link=subfolder_link
        )

    completed_videos = []
    failed_videos = []

    for idx, entry in enumerate(entries, start=1):
        v_id = entry.get("id")
        v_url = entry.get("url") or (f"https://www.youtube.com/watch?v={v_id}" if v_id else "")
        v_raw_title = entry.get("title") or f"Tập {idx}"
        v_title = f"{idx:02d} - {clean_filename(v_raw_title)}"

        pct = int((idx - 1) / total_count * 100)
        progress_str = f"[{idx}/{total_count}] {pct}%"

        logger.info(f"📥 [{idx}/{total_count}] Processing: {v_title} ({v_url})")

        if sheet_row:
            gsheet_helper.update_media_task_status(sheet_row, progress=f"{progress_str} - Đang tải tập {idx}")

        if chat_id and status_msg_id:
            telegram_helper.edit_message(
                chat_id, status_msg_id,
                f"📥 <b>[Cloud Runner: {RUNNER_REPO}] Đang xử lý Playlist ({progress_str}):</b>\n"
                f"📋 <b>Playlist:</b> <code>{html.escape(pl_title)}</code>\n"
                f"🎬 <b>Tập {idx}/{total_count}:</b> <code>{html.escape(v_title)}</code>\n"
                f"📁 <b>Thư mục Drive:</b> <a href=\"{subfolder_link}\">Mở Thư Mục</a>"
            )

        v_path = os.path.join(temp_dir, f"{v_title}.mp4")
        a_path = os.path.join(temp_dir, f"{v_title}.mp3")

        # Resilient Download per Video
        try:
            cmd_v = get_ytdlp_cmd() + [
                "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "-o", v_path,
                v_url
            ]
            res_v = subprocess.run(cmd_v, capture_output=True, text=True)
            if not os.path.exists(v_path) or os.path.getsize(v_path) == 0:
                logger.warning(f"⚠️ Video #{idx} failed: {res_v.stderr[:100]}")
                failed_videos.append((idx, v_raw_title, res_v.stderr[:60]))
                continue

            # Upload video
            gdrive_helper.upload_file_to_drive(v_path, f"{v_title}.mp4", subfolder_id, mime_type="video/mp4", owner_email=OWNER_EMAIL)

            # Optional Audio extraction
            cmd_a = get_ytdlp_cmd() + ["-x", "--audio-format", "mp3", "-o", a_path, v_url]
            subprocess.run(cmd_a, capture_output=True)
            if os.path.exists(a_path) and os.path.getsize(a_path) > 1024:
                gdrive_helper.upload_file_to_drive(a_path, f"{v_title}.mp3", subfolder_id, mime_type="audio/mpeg", owner_email=OWNER_EMAIL)

            completed_videos.append(v_title)
        except Exception as ve:
            logger.warning(f"⚠️ Exception on video #{idx}: {ve}")
            failed_videos.append((idx, v_raw_title, str(ve)[:60]))
        finally:
            # Clean local files after upload to save disk
            for p in [v_path, a_path]:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

    # 3. Final Summary
    final_status = "Completed" if not failed_videos else "Completed (With Warnings)"
    final_progress = f"{len(completed_videos)}/{total_count} (100%)"

    if sheet_row:
        gsheet_helper.update_media_task_status(
            sheet_row,
            status=final_status,
            progress=final_progress,
            title=pl_title,
            drive_link=subfolder_link
        )

    if chat_id:
        summary_msg = (
            f"🎉 <b>ĐÃ HOÀN THÀNH PLAYLIST!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>Playlist:</b> <code>{html.escape(pl_title)}</code>\n"
            f"📊 <b>Kết quả:</b> <code>{len(completed_videos)}/{total_count} video thành công</code>\n"
            f"⚙️ <b>Cloud Runner:</b> <code>{html.escape(RUNNER_REPO)}</code>\n"
            f"📁 <b>Google Drive:</b> <a href=\"{subfolder_link}\">Mở Thư Mục Playlist</a>\n"
        )
        if failed_videos:
            summary_msg += f"\n⚠️ <b>Video không tải được ({len(failed_videos)}):</b>\n"
            for f_idx, f_t, f_err in failed_videos[:5]:
                summary_msg += f"• #{f_idx}: {html.escape(f_t[:30])} (<code>{html.escape(f_err[:30])}</code>)\n"
            if len(failed_videos) > 5:
                summary_msg += f"• ... và {len(failed_videos) - 5} video khác.\n"

        if status_msg_id:
            telegram_helper.edit_message(chat_id, status_msg_id, summary_msg)
        else:
            telegram_helper.send_message(chat_id, summary_msg, thread_id=thread_id)

    return True


def handle_channel(task: Dict[str, Any], temp_dir: str) -> bool:
    """Handles /wf1 YouTube Channel download."""
    url = task["url"]
    tab_url = url.rstrip("/") + "/playlists" if not url.endswith("/playlists") else url
    task["url"] = tab_url
    return handle_playlist(task, temp_dir)


def handle_tiktok(task: Dict[str, Any], temp_dir: str) -> bool:
    """Handles /wf4 TikTok video without watermark (MP4 + MP3) & sends to Telegram."""
    url = task["url"]
    chat_id = task["chat_id"]
    thread_id = task["thread_id"]
    status_msg_id = task["status_msg_id"]
    sheet_row = task["sheet_row"]
    folder_id = task["drive_folder_id"]

    logger.info(f"📱 Processing TikTok: {url}")
    ts = int(time.time())
    title = f"TikTok_{ts}"

    if sheet_row:
        gsheet_helper.update_media_task_status(sheet_row, title=title, progress="20% (Đang tải TikTok)")

    if chat_id and status_msg_id:
        telegram_helper.edit_message(
            chat_id, status_msg_id,
            f"📥 <b>[Cloud Runner: {RUNNER_REPO}] Đang tải TikTok không watermark...</b>\n🔗 <code>{html.escape(url)}</code>"
        )

    v_path = os.path.join(temp_dir, f"{title}.mp4")
    a_path = os.path.join(temp_dir, f"{title}.mp3")

    cmd_v = get_ytdlp_cmd() + ["-o", v_path, url]
    subprocess.run(cmd_v, capture_output=True)

    cmd_a = get_ytdlp_cmd() + ["-x", "--audio-format", "mp3", "-o", a_path, url]
    subprocess.run(cmd_a, capture_output=True)

    if not os.path.exists(v_path) or os.path.getsize(v_path) == 0:
        if sheet_row:
            gsheet_helper.update_media_task_status(sheet_row, status="Error (TikTok Download Failed)", progress="Lỗi tải TikTok")
        if chat_id and status_msg_id:
            telegram_helper.edit_message(chat_id, status_msg_id, f"❌ <b>Không thể tải TikTok. Vui lòng kiểm tra link.</b>")
        return False

    v_link = gdrive_helper.upload_file_to_drive(v_path, f"{title}.mp4", folder_id, mime_type="video/mp4", owner_email=OWNER_EMAIL)
    if os.path.exists(a_path) and os.path.getsize(a_path) > 1024:
        gdrive_helper.upload_file_to_drive(a_path, f"{title}.mp3", folder_id, mime_type="audio/mpeg", owner_email=OWNER_EMAIL)

    # Deliver directly to Telegram chat
    if chat_id:
        caption = f"📱 <b>TikTok Video:</b>\n🔗 <a href=\"{url}\">Xem Link Gốc</a>\n📁 <a href=\"{v_link}\">Google Drive</a>"
        telegram_helper.send_video(chat_id, v_path, caption=caption, thread_id=thread_id)

    if sheet_row:
        gsheet_helper.update_media_task_status(sheet_row, status="Completed", progress="100%", title=title, drive_link=v_link)

    if chat_id and status_msg_id:
        telegram_helper.edit_message(
            chat_id, status_msg_id,
            f"🎉 <b>ĐÃ HOÀN THÀNH TẢI TIKTOK!</b>\n📁 <b>Google Drive:</b> <a href=\"{v_link}\">Mở File MP4</a>"
        )

    return True


def handle_fb_insta(task: Dict[str, Any], temp_dir: str) -> bool:
    """Handles /wf6 Facebook & Instagram videos, reels, photos, and carousels."""
    url = task["url"]
    chat_id = task["chat_id"]
    thread_id = task["thread_id"]
    status_msg_id = task["status_msg_id"]
    sheet_row = task["sheet_row"]
    folder_id = FB_IG_FOLDER_ID or task["drive_folder_id"]

    platform = "Facebook" if "facebook" in url.lower() or "fb." in url.lower() else "Instagram"
    ts = int(time.time())
    title = f"{platform}_{ts}"

    if sheet_row:
        gsheet_helper.update_media_task_status(sheet_row, title=title, progress=f"20% (Đang phân tích {platform})")

    if chat_id and status_msg_id:
        telegram_helper.edit_message(
            chat_id, status_msg_id,
            f"🔎 <b>[Cloud Runner: {RUNNER_REPO}] Đang tải media {platform}...</b>\n🔗 <code>{html.escape(url)}</code>"
        )

    # 1. Download via yt-dlp
    out_tmpl = os.path.join(temp_dir, f"{title}_%(id)s.%(ext)s")
    dl_cmd = get_ytdlp_cmd() + ["-o", out_tmpl, "--no-playlist", url]
    subprocess.run(dl_cmd, capture_output=True)

    downloaded = [
        os.path.join(temp_dir, f) for f in os.listdir(temp_dir)
        if f.startswith(title) and os.path.getsize(os.path.join(temp_dir, f)) > 1024
    ]

    # 2. Fallback meta tag scraper if yt-dlp produced 0 files
    if not downloaded:
        try:
            r = requests.get(url, headers={"User-Agent": "facebookexternalhit/1.1"}, timeout=15)
            html_txt = r.text
            og_v = re.search(r'<meta property="og:video" content="([^"]+)"', html_txt)
            og_i = re.search(r'<meta property="og:image" content="([^"]+)"', html_txt)
            if og_v:
                v_f = os.path.join(temp_dir, f"{title}.mp4")
                r_v = requests.get(og_v.group(1).replace("&amp;", "&"), timeout=30)
                if r_v.status_code == 200:
                    with open(v_f, "wb") as f:
                        f.write(r_v.content)
                    downloaded.append(v_f)
            elif og_i:
                i_f = os.path.join(temp_dir, f"{title}.jpg")
                r_i = requests.get(og_i.group(1).replace("&amp;", "&"), timeout=30)
                if r_i.status_code == 200:
                    with open(i_f, "wb") as f:
                        f.write(r_i.content)
                    downloaded.append(i_f)
        except Exception as e:
            logger.warning(f"Meta tag scraper fallback error: {e}")

    if not downloaded:
        if sheet_row:
            gsheet_helper.update_media_task_status(sheet_row, status=f"Error ({platform} Download Failed)", progress="Không tìm thấy media")
        if chat_id and status_msg_id:
            telegram_helper.edit_message(chat_id, status_msg_id, f"❌ <b>Không thể tải media {platform}. Vui lòng kiểm tra link công khai.</b>")
        return False

    # 3. Upload to Google Drive and deliver to Telegram
    uploaded_links = []
    for f_path in downloaded:
        f_name = os.path.basename(f_path)
        link = gdrive_helper.upload_file_to_drive(f_path, f_name, folder_id, owner_email=OWNER_EMAIL)
        uploaded_links.append(link)

    # Deliver to Telegram
    if chat_id:
        videos = [f for f in downloaded if f.endswith((".mp4", ".mkv", ".mov"))]
        photos = [f for f in downloaded if f.endswith((".jpg", ".jpeg", ".png", ".webp"))]
        caption = f"🎬 <b>{platform} Media:</b>\n🔗 <a href=\"{url}\">Xem Link Gốc</a>"

        for v in videos:
            telegram_helper.send_video(chat_id, v, caption=caption, thread_id=thread_id)
        if len(photos) == 1:
            telegram_helper.send_photo(chat_id, photos[0], caption=caption, thread_id=thread_id)
        elif len(photos) > 1:
            telegram_helper.send_media_group(chat_id, photos, caption=caption, thread_id=thread_id)

    primary_link = uploaded_links[0] if uploaded_links else f"https://drive.google.com/drive/folders/{folder_id}"
    if sheet_row:
        gsheet_helper.update_media_task_status(sheet_row, status="Completed", progress="100%", title=title, drive_link=primary_link)

    if chat_id and status_msg_id:
        telegram_helper.edit_message(
            chat_id, status_msg_id,
            f"🎉 <b>ĐÃ HOÀN THÀNH TẢI {platform.upper()}!</b>\n"
            f"📊 <b>Số file:</b> <code>{len(downloaded)}</code>\n"
            f"📁 <b>Google Drive:</b> <a href=\"{primary_link}\">Mở Google Drive</a>"
        )

    return True


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================

def main():
    logger.info("🚀 Media Processor Cloud Runner initialized.")
    task = parse_task_payload()
    logger.info(f"Task Config: id={task['task_id']}, cmd={task['cmd']}, type={task['media_type']}, url={task['url']}")

    if not task["url"]:
        logger.error("❌ No URL provided in task payload!")
        sys.exit(1)

    temp_dir = os.path.join(os.getcwd(), f"temp_{task['task_id']}")
    os.makedirs(temp_dir, exist_ok=True)

    success = False
    try:
        cmd = task["cmd"]
        m_type = task["media_type"]

        if cmd == "/wf3" or m_type == "single":
            success = handle_single_video(task, temp_dir)
        elif cmd == "/wf2" or m_type == "playlist":
            success = handle_playlist(task, temp_dir)
        elif cmd == "/wf1" or m_type == "channel":
            success = handle_channel(task, temp_dir)
        elif cmd == "/wf4" or m_type == "tiktok":
            success = handle_tiktok(task, temp_dir)
        elif cmd == "/wf6" or m_type == "fb_insta":
            success = handle_fb_insta(task, temp_dir)
        else:
            logger.warning(f"Unrecognized command {cmd}, defaulting to single video...")
            success = handle_single_video(task, temp_dir)
    except Exception as e:
        logger.error(f"💥 Unhandled exception in media processor: {e}", exc_info=True)
        if task.get("sheet_row"):
            gsheet_helper.update_media_task_status(task["sheet_row"], status=f"Error ({str(e)[:40]})", progress="Lỗi thực thi Cloud Runner")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    logger.info(f"🏁 Media Processor completed with status={'SUCCESS' if success else 'FAILED'}")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
