"""Meta platforms: Instagram Reels, Facebook Page Reels, Threads.

- Instagram / Facebook: local file upload through rupload.facebook.com (resumable protocol).
- Threads: only accepts a public video_url, so we pass the Instagram Reel's CDN media_url
  (Instagram must be posted first in the same run) or PUBLIC_BASE_URL + relative path.
"""

import os
from typing import Dict, Optional

import requests
from loguru import logger

from quote_clips.publish.http import TIMEOUT, PublishError, check, poll

GRAPH = "https://graph.facebook.com/" + os.getenv("META_GRAPH_VERSION", "v25.0")
RUPLOAD = "https://rupload.facebook.com"
THREADS = "https://graph.threads.net/v1.0"


def _env(cfg: Dict, key: str) -> str:
    name = cfg[key]
    value = os.getenv(name, "")
    if not value:
        raise PublishError(f"environment variable {name} is not set")
    return value


def _upload_bytes(url: str, token: str, video_path: str, what: str) -> None:
    size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        resp = requests.post(url, data=f, timeout=TIMEOUT * 5, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
            "Content-Type": "application/octet-stream",
        })
    check(resp, what)


def post_instagram(video_path: str, caption: Dict, cfg: Dict) -> Dict:
    token, ig = _env(cfg, "token_env"), cfg["ig_user_id"]
    auth = {"Authorization": f"Bearer {token}"}
    container = check(requests.post(f"{GRAPH}/{ig}/media", headers=auth, timeout=TIMEOUT, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption["instagram"][:2200],
        "share_to_feed": "true",
    }), "Instagram container")
    cid = container["id"]
    _upload_bytes(container.get("uri") or f"{RUPLOAD}/ig-api-upload/{GRAPH.rsplit('/', 1)[1]}/{cid}",
                  token, video_path, "Instagram upload")
    poll(lambda: check(requests.get(f"{GRAPH}/{cid}", headers=auth, timeout=TIMEOUT,
                                    params={"fields": "status_code,status"}), "Instagram status"),
         lambda d: True if d.get("status_code") == "FINISHED" else (False if d.get("status_code") in ("ERROR", "EXPIRED") else None),
         "Instagram processing")
    media_id = check(requests.post(f"{GRAPH}/{ig}/media_publish", headers=auth, timeout=TIMEOUT,
                                   data={"creation_id": cid}), "Instagram publish")["id"]
    info = check(requests.get(f"{GRAPH}/{media_id}", headers=auth, timeout=TIMEOUT,
                              params={"fields": "permalink,media_url"}), "Instagram media info")
    logger.info(f"Instagram: {info.get('permalink')}")
    return {"id": media_id, "url": info.get("permalink", ""), "media_url": info.get("media_url", "")}


def post_facebook(video_path: str, caption: Dict, cfg: Dict) -> Dict:
    token, page = _env(cfg, "token_env"), cfg["page_id"]
    start = check(requests.post(f"{GRAPH}/{page}/video_reels", timeout=TIMEOUT,
                                data={"upload_phase": "start", "access_token": token}), "Facebook reel start")
    vid = start["video_id"]
    _upload_bytes(start.get("upload_url") or f"{RUPLOAD}/video-upload/{GRAPH.rsplit('/', 1)[1]}/{vid}",
                  token, video_path, "Facebook upload")
    check(requests.post(f"{GRAPH}/{page}/video_reels", timeout=TIMEOUT, data={
        "access_token": token, "video_id": vid, "upload_phase": "finish",
        "video_state": "PUBLISHED", "description": caption["facebook"][:5000],
    }), "Facebook reel publish")
    logger.info(f"Facebook: reel video_id={vid}")
    return {"id": vid, "url": f"https://www.facebook.com/reel/{vid}"}


def post_threads(video_path: str, caption: Dict, cfg: Dict, video_url: Optional[str]) -> Dict:
    if not video_url:
        raise PublishError("Threads needs a public video URL: post Instagram first or set PUBLIC_BASE_URL")
    token, uid = _env(cfg, "token_env"), cfg["user_id"]
    cid = check(requests.post(f"{THREADS}/{uid}/threads", timeout=TIMEOUT, data={
        "media_type": "VIDEO", "video_url": video_url, "text": caption["threads"][:500], "access_token": token,
    }), "Threads container")["id"]
    poll(lambda: check(requests.get(f"{THREADS}/{cid}", timeout=TIMEOUT,
                                    params={"fields": "status,error_message", "access_token": token}), "Threads status"),
         lambda d: True if d.get("status") == "FINISHED" else (False if d.get("status") in ("ERROR", "EXPIRED") else None),
         "Threads processing", interval=30, timeout=600)
    tid = check(requests.post(f"{THREADS}/{uid}/threads_publish", timeout=TIMEOUT,
                              data={"creation_id": cid, "access_token": token}), "Threads publish")["id"]
    logger.info(f"Threads: post id={tid}")
    return {"id": tid}
