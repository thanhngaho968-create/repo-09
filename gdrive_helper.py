import os
import io
import json
import base64
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

logger = logging.getLogger(__name__)

def get_drive_service():
    sa_b64 = os.environ.get("GDRIVE_SA_BASE64", "")
    if not sa_b64:
        raise ValueError("Missing GDRIVE_SA_BASE64 environment variable")
    sa_json = base64.b64decode(sa_b64).decode("utf-8")
    sa_info = json.loads(sa_json)
    
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def get_or_create_folder(folder_name, parent_id):
    service = get_drive_service()
    query = f"name = '{folder_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = service.files().list(q=query, fields="files(id, name)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    
    meta = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]

def upload_file_to_drive(local_path, file_name, parent_folder_id, mime_type="application/octet-stream", owner_email=None):
    service = get_drive_service()
    meta = {
        "name": file_name,
        "parents": [parent_folder_id]
    }
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
    file_obj = service.files().create(
        body=meta,
        media_body=media,
        fields="id, name, webViewLink, webContentLink"
    ).execute()

    if owner_email:
        try:
            service.permissions().create(
                fileId=file_obj["id"],
                body={"type": "user", "role": "writer", "emailAddress": owner_email},
                sendNotificationEmail=False
            ).execute()
        except Exception:
            pass

    return file_obj.get("webViewLink", "") or f"https://drive.google.com/file/d/{file_obj['id']}/view"
