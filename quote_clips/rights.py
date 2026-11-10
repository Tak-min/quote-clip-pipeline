"""quote_clips/rights.py — per-work footage permissions (research/copyright/DECISION.md v7).

Anime / film / TV footage and its original audio are used ONLY for works recorded in
rights/permissions.json with the rights holder's permission. An empty or incomplete entry
means "not permitted": the daily job then falls back to the stock-footage scene short.

Entry (key = app work id, e.g. "haikyu"):
{
  "rights_holder": "「ハイキュー!!」製作委員会",        # who owns the footage (not a fan uploader)
  "granted_by": "name / title of the person who said yes",
  "granted_on": "2026-11-10",
  "platforms": ["youtube", "tiktok", "instagram"],    # where the clips may be posted
  "commercial": true,                                  # app promotion is commercial use
  "evidence": "rights/evidence/haikyu.pdf",            # mail / contract kept for take-down disputes
  "credit": "©古舘春一／集英社・「ハイキュー!!」製作委員会",
  "sources": [{"url": "https://www.youtube.com/watch?v=...", "note": "official PV"}]
}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
PERMISSIONS = Path(os.getenv("RIGHTS_FILE") or ROOT / "rights" / "permissions.json")
REQUIRED = ("rights_holder", "granted_by", "granted_on", "credit")


def load(path: Path = PERMISSIONS) -> Dict[str, Dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in (data.get("works") or {}).items() if isinstance(v, dict)}


def problems(entry: Dict, platforms: List[str]) -> List[str]:
    """Why an entry does not permit use on these platforms (empty list = permitted)."""
    out = [f"missing {k}" for k in REQUIRED if not str(entry.get(k) or "").strip()]
    if entry.get("commercial") is not True:
        out.append("commercial use not granted")
    missing = [p for p in platforms if p not in (entry.get("platforms") or [])]
    if missing:
        out.append(f"platforms not granted: {', '.join(missing)}")
    if not [s for s in entry.get("sources") or [] if (s or {}).get("url")]:
        out.append("no source url")
    return out


def permitted(platforms: List[str], path: Path = PERMISSIONS) -> Dict[str, Dict]:
    return {k: v for k, v in load(path).items() if not problems(v, platforms)}


def find(work: str, platforms: List[str], path: Path = PERMISSIONS) -> Optional[Dict]:
    """Permitted entry for a work id or its display name (ja/en), else None."""
    for key, v in permitted(platforms, path).items():
        if work and work in (key, v.get("work_ja"), v.get("work_en")):
            return {**v, "work_id": key}
    return None
