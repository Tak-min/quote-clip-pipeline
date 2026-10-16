"""TikTok Content Posting API — Direct Post with FILE_UPLOAD.

Unaudited API clients can only post as SELF_ONLY (private) until the app passes TikTok's audit.
"""

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from loguru import logger

from quote_clips.publish.http import TIMEOUT, PublishError, check, poll

API = "https://open.tiktokapis.com/v2"
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024


def chunk_plan(size: int, chunk: int = 10 * 1024 * 1024) -> Tuple[int, int, List[Tuple[int, int]]]:
    """(chunk_size, total_chunk_count, [(first_byte, last_byte)]) per TikTok's media transfer rules.

    <5MB → one chunk equal to the file; otherwise floor(size/chunk) chunks, remainder merged into the last.
    """
    if size < MIN_CHUNK:
        return size, 1, [(0, size - 1)]
    chunk = max(MIN_CHUNK, min(chunk, MAX_CHUNK))
    count = max(1, size // chunk)
    ranges = [(i * chunk, (i + 1) * chunk - 1) for i in range(count)]
    ranges[-1] = (ranges[-1][0], size - 1)
    return chunk, count, ranges


def _token(cfg: Dict) -> str:
    """Return a valid access token, refreshing with the stored refresh_token when expired."""
    path = Path(cfg["token_file"])
    tok = json.loads(path.read_text())
    if tok.get("expires_at", 0) - 300 > time.time():
        return tok["access_token"]
    resp = requests.post(f"{API}/oauth/token/", data={
        "client_key": os.environ["TIKTOK_CLIENT_KEY"],
        "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
    }, timeout=TIMEOUT)
    new = check(resp, "TikTok token refresh")
    tok.update(access_token=new["access_token"], refresh_token=new.get("refresh_token", tok["refresh_token"]),
               expires_at=time.time() + int(new.get("expires_in", 86400)))
    path.write_text(json.dumps(tok))
    return tok["access_token"]


def post(video_path: str, caption: Dict, cfg: Dict) -> Dict:
    headers = {"Authorization": f"Bearer {_token(cfg)}", "Content-Type": "application/json; charset=UTF-8"}

    info = check(requests.post(f"{API}/post/publish/creator_info/query/", headers=headers, timeout=TIMEOUT),
                 "TikTok creator_info")["data"]
    privacy = cfg.get("privacy_level", "PUBLIC_TO_EVERYONE")
    if privacy not in info.get("privacy_level_options", []):
        logger.warning(f"TikTok: {privacy} not allowed for this client/account; using SELF_ONLY")
        privacy = "SELF_ONLY"

    size = os.path.getsize(video_path)
    chunk, count, ranges = chunk_plan(size)
    init = check(requests.post(f"{API}/post/publish/video/init/", headers=headers, timeout=TIMEOUT, json={
        "post_info": {
            "title": caption["tiktok"][:2200],
            "privacy_level": privacy,
            "disable_duet": False, "disable_stitch": False, "disable_comment": False,
            "brand_content_toggle": False, "brand_organic_toggle": False,
        },
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count},
    }), "TikTok init")["data"]

    with open(video_path, "rb") as f:
        for first, last in ranges:
            f.seek(first)
            body = f.read(last - first + 1)
            resp = requests.put(init["upload_url"], data=body, timeout=TIMEOUT * 5, headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(body)),
                "Content-Range": f"bytes {first}-{last}/{size}",
            })
            if resp.status_code not in (200, 201, 206):
                raise PublishError(f"TikTok chunk upload failed ({resp.status_code}): {resp.text[:300]}")

    publish_id = init["publish_id"]
    result = poll(
        lambda: check(requests.post(f"{API}/post/publish/status/fetch/", headers=headers, timeout=TIMEOUT,
                                    json={"publish_id": publish_id}), "TikTok status")["data"],
        lambda d: True if d.get("status") == "PUBLISH_COMPLETE" else (False if d.get("status") == "FAILED" else None),
        "TikTok publish",
    )
    post_ids = result.get("publicaly_available_post_id") or result.get("publicly_available_post_id") or []
    logger.info(f"TikTok: published ({privacy}) publish_id={publish_id}")
    return {"id": str(post_ids[0]) if post_ids else publish_id, "privacy": privacy}
