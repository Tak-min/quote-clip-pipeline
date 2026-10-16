"""YouTube Shorts upload (remixed from youtube-shorts-factory-product/scripts/youtube_uploader.py)."""

import os
from pathlib import Path
from typing import Dict

from loguru import logger

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _credentials(cfg: Dict):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_file = cfg["token_file"]
    creds = Credentials.from_authorized_user_file(token_file, SCOPES) if os.path.exists(token_file) else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        # First run only: opens a browser for consent. Use `python -m quote_clips.publish auth youtube`.
        flow = InstalledAppFlow.from_client_secrets_file(cfg["client_secret_file"], SCOPES)
        creds = flow.run_local_server(port=0)
    Path(token_file).parent.mkdir(parents=True, exist_ok=True)
    Path(token_file).write_text(creds.to_json())
    return creds


def post(video_path: str, caption: Dict, cfg: Dict) -> Dict:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    youtube = build("youtube", "v3", credentials=_credentials(cfg), cache_discovery=False)
    body = {
        "snippet": {
            "title": caption["title"][:100],
            "description": caption["description"][:5000],
            "tags": [t.lstrip("#") for t in caption["hashtags"]][:30],
            "categoryId": str(cfg.get("category_id", "22")),  # People & Blogs
            "defaultLanguage": caption["lang"],
        },
        "status": {"privacyStatus": cfg.get("privacy", "public"), "selfDeclaredMadeForKids": False},
    }
    request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    vid = response["id"]
    logger.info(f"YouTube: https://youtube.com/shorts/{vid}")
    return {"id": vid, "url": f"https://youtube.com/shorts/{vid}"}
