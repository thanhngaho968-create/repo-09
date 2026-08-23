import os
import requests
import json
import logging

logger = logging.getLogger(__name__)

CF_RELAY_URL = os.environ.get("CF_RELAY_URL", "")
CF_RELAY_SECRET = os.environ.get("CF_RELAY_SECRET", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

def make_tg_request(method, data=None, files=None):
    if CF_RELAY_URL:
        url = f"{CF_RELAY_URL.rstrip('/')}/relay/{method}"
        headers = {"X-Relay-Secret": CF_RELAY_SECRET}
        try:
            res = requests.post(url, headers=headers, data=data, files=files, timeout=120)
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.warning(f"Relay failed: {e}, falling back to direct TG API")
            
    if BOT_TOKEN:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
        res = requests.post(url, data=data, files=files, timeout=120)
        return res.json()
    
    return {"ok": False, "error": "No valid bot token or relay"}

def send_message(chat_id, text, parse_mode="HTML", reply_to_message_id=None):
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    return make_tg_request("sendMessage", data=data)

def send_video(chat_id, video_path, caption="", thumb_path=None, duration=0, width=0, height=0, reply_to_message_id=None):
    data = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
        "supports_streaming": True
    }
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    if duration:
        data["duration"] = int(duration)
    if width and height:
        data["width"] = int(width)
        data["height"] = int(height)
        
    files = {"video": open(video_path, "rb")}
    if thumb_path and os.path.exists(thumb_path):
        files["thumb"] = open(thumb_path, "rb")
        
    return make_tg_request("sendVideo", data=data, files=files)

def send_document(chat_id, doc_path, caption="", thumb_path=None, reply_to_message_id=None):
    data = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML"
    }
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
        
    files = {"document": open(doc_path, "rb")}
    if thumb_path and os.path.exists(thumb_path):
        files["thumb"] = open(thumb_path, "rb")
        
    return make_tg_request("sendDocument", data=data, files=files)

def send_audio(chat_id, audio_path, caption="", title="", performer="", thumb_path=None, duration=0, reply_to_message_id=None):
    data = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
        "title": title,
        "performer": performer
    }
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    if duration:
        data["duration"] = int(duration)
        
    files = {"audio": open(audio_path, "rb")}
    if thumb_path and os.path.exists(thumb_path):
        files["thumb"] = open(thumb_path, "rb")
        
    return make_tg_request("sendAudio", data=data, files=files)
