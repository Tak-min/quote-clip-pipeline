"""Small HTTP helpers shared by the Meta / TikTok clients."""

import time
from typing import Any, Callable, Dict, Optional

import requests

TIMEOUT = 120


class PublishError(RuntimeError):
    pass


def check(resp: requests.Response, what: str) -> Dict[str, Any]:
    """Raise PublishError with the API's own message on failure; return JSON on success."""
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:500]}
    if resp.status_code >= 400 or (isinstance(data, dict) and data.get("error") and not _tiktok_ok(data)):
        raise PublishError(f"{what} failed ({resp.status_code}): {str(data)[:500]}")
    return data


def _tiktok_ok(data: Dict[str, Any]) -> bool:
    # TikTok always returns an "error" object; code "ok" means success.
    err = data.get("error")
    return isinstance(err, dict) and err.get("code") == "ok"


def poll(fetch: Callable[[], Dict[str, Any]], done: Callable[[Dict[str, Any]], Optional[bool]],
         what: str, interval: float = 10.0, timeout: float = 600.0) -> Dict[str, Any]:
    """Call fetch() until done(result) is True (finished) or False (failed)."""
    deadline = time.monotonic() + timeout
    while True:
        result = fetch()
        state = done(result)
        if state is True:
            return result
        if state is False:
            raise PublishError(f"{what} failed: {str(result)[:500]}")
        if time.monotonic() > deadline:
            raise PublishError(f"{what} timed out: {str(result)[:300]}")
        time.sleep(interval)
