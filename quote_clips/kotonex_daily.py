"""quote_clips/kotonex_daily.py — daily KOTONEX card shorts → sns-poster queue (runs on the Pi).

For each of today's JST slots that is still in the future and not yet queued:
  pick an unused quote from the app data (mostly historical figures, one short anime citation),
  render an ENGLISH card short with quote_clips.run --langs en (card mode only — no third-party footage),
  build per-platform English captions (app link, BGM credit, 3–5 hashtags) and
  `sns-poster enqueue --at <slot>` it for YouTube / TikTok / Instagram.

Idempotent: safe to run every hour (cron). State lives in state/ (used quote ids, per-day schedule).

    .venv/bin/python -m quote_clips.kotonex_daily              # fill today's remaining slots
    .venv/bin/python -m quote_clips.kotonex_daily --now        # render + queue one post due immediately
    .venv/bin/python -m quote_clips.kotonex_daily --dry-run    # pick + caption only (no render, no enqueue)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
JST = dt.timezone(dt.timedelta(hours=9))
APP_URL = "https://apps.apple.com/us/app/id6827862117"
LANG = "en"   # English account copy (cards, captions, hashtags)

# (time, kind, preferred app scenes)
SLOTS = [
    ("07:30", "figure", ("monday", "study")),
    ("12:00", "figure", ("big_moment", "training", "study")),
    ("17:30", "figure", ("training", "big_moment")),
    ("20:00", "anime", ()),
    ("22:00", "figure", ("setback", "slacking")),
]
ANIME_MAX_CHARS = 40       # anime: short text citation only (Japanese length)
ANIME_MAX_EN = 100
FIGURE_MAX_EN = 160
BLURB = ("KOTONEX — quotes from anime and history's greats, with the scene, read-aloud and widgets. "
         "Speaker and source always shown.")
ANIME_NOTE = "Short quotation with the source credited. KOTONEX is not affiliated with the work or its creators."


# ── quote choice ─────────────────────────────────────────────────────────


def load_pool(data_dir: Path) -> Dict[str, List[Dict]]:
    figs = json.loads((data_dir / "quotes_figures.json").read_text(encoding="utf-8"))
    anime = json.loads((data_dir / "quotes_anime.json").read_text(encoding="utf-8"))
    def has_en(q: Dict) -> bool:  # only quotes whose English wording/source already exist in the app data
        return all((q.get(k) or "").strip() for k in ("text_en", "speaker_en", "source_en"))

    return {
        # living people are never used (it would look like they endorse the app)
        "figure": [q for q in figs if has_en(q) and not q.get("speaker_living") and len(q["text_en"]) <= FIGURE_MAX_EN],
        "anime": [q for q in anime if has_en(q) and len(q["text_ja"]) <= ANIME_MAX_CHARS
                  and len(q["text_en"]) <= ANIME_MAX_EN],
    }


def pick(pool: List[Dict], used: set, rng: random.Random, prefer=(), avoid_speakers=()) -> Dict:
    fresh = [q for q in pool if q["id"] not in used]
    if not fresh:  # whole pool used once: start a new cycle
        used.difference_update(q["id"] for q in pool)
        fresh = list(pool)
    cand = [q for q in fresh if q["speaker_ja"] not in avoid_speakers] or fresh
    pref = [q for q in cand if set(q.get("scenes") or []) & set(prefer)]
    return rng.choice(pref or cand)


# ── captions ─────────────────────────────────────────────────────────────


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_posts(meta: Dict, is_anime: bool) -> Dict:
    quote = meta.get("quote", "").replace("\n", "")
    speaker = meta.get("speaker", "")
    cite = meta.get("cite", "")              # "— Speaker (Work) citation"
    desc = meta.get("description", "")
    music = next((ln for ln in desc.splitlines() if ln.startswith("Music:")), "")
    tags = [t for t in meta.get("hashtags", []) if t != "#KOTONEX"][:4] + ["#KOTONEX"]
    head = f"\u201c{quote}\u201d\n{cite}"
    tail = "\n".join(x for x in (music, ANIME_NOTE if is_anime else "") if x) + "\n\n" + " ".join(tags)
    yt = f"{head}\n\n{BLURB}\nGet KOTONEX on the App Store: {APP_URL}\n\n{tail}"
    ig = f"{head}\n\n{BLURB}\nOn the App Store — link in bio\n{APP_URL}\n\n{tail}"
    tt = f"{head}\n\n{BLURB}\nOn the App Store — link in bio.\n\n{tail}"
    suffix = f" \u2014 {speaker} #Shorts"
    title = f"\u201c{_fit(quote, 90 - len(suffix) - 2)}\u201d{suffix}"
    return {
        "title": title,
        "youtube": {"title": title, "description": _fit(yt, 5000), "tags": [t.lstrip("#") for t in tags],
                    "privacy": "public"},
        "tiktok": {"caption": _fit(tt, 2200), "privacy_level": "PUBLIC_TO_EVERYONE"},
        "instagram": {"caption": _fit(ig, 2200)},
    }


# ── render + enqueue ─────────────────────────────────────────────────────


def render(qid: str, out_dir: Path) -> Path:
    found = sorted((out_dir / LANG / "cards").glob(f"{qid}_*.mp4"))
    if not found:
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write(f"mode,quote_id\ncard,{qid}\n")
            csv_path = f.name
        try:
            subprocess.run([sys.executable, "-m", "quote_clips.run", "--sources", csv_path, "--langs", LANG,
                            "--out-dir", str(out_dir)], cwd=ROOT, check=True)
        finally:
            os.unlink(csv_path)
        found = sorted((out_dir / LANG / "cards").glob(f"{qid}_*.mp4"))
    if not found or not found[0].with_suffix(".json").exists():
        raise RuntimeError(f"render produced no video for {qid}")
    return found[0]


def enqueue(video: Path, posts: Dict, at: dt.datetime, meta: Dict) -> str:
    poster = os.path.expanduser(os.getenv("SNS_POSTER_BIN", "~/sns-poster/bin/sns-poster"))
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({k: posts[k] for k in ("youtube", "tiktok", "instagram")} | {"meta": meta}, f, ensure_ascii=False)
        opts = f.name
    try:
        out = subprocess.run([poster, "enqueue", str(video), "--title", posts["title"],
                              "--caption", posts["instagram"]["caption"], "--at", at.isoformat(),
                              "--platforms", "youtube,tiktok,instagram", "--options-json", opts],
                             capture_output=True, text=True, check=True)
    finally:
        os.unlink(opts)
    return out.stdout.strip().splitlines()[-1]


# ── main ─────────────────────────────────────────────────────────────────


def _load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def run(now: dt.datetime, data_dir: Path, dry_run: bool = False, immediate: bool = False,
        lead_min: int = 5) -> List[Dict]:
    day = now.date().isoformat()
    sched_path = STATE / f"schedule-{day}.json"
    used_path = STATE / "used.json"
    sched = _load(sched_path, {})
    used = set(_load(used_path, []))
    pool = load_pool(data_dir)
    rng = random.Random(f"kotonex:{day}:{len(sched)}")
    today_speakers = {v.get("speaker") for v in sched.values()}
    slots = ([("now-" + now.strftime("%H%M"), "figure", ())] if immediate else SLOTS)
    done = []
    for hhmm, kind, prefer in slots:
        if hhmm in sched and sched[hhmm].get("job"):
            continue
        at = now if immediate else dt.datetime.fromisoformat(f"{day}T{hhmm}:00+09:00")
        if not immediate and at < now + dt.timedelta(minutes=lead_min):
            continue  # slot already passed (or too close to render in time)
        entry = sched.get(hhmm) or {}
        q = next((x for x in pool[kind] if x["id"] == entry.get("quote_id")), None) or \
            pick(pool[kind], used, rng, prefer, today_speakers)
        entry.update(quote_id=q["id"], speaker=q["speaker_ja"], at=at.isoformat())
        sched[hhmm] = entry
        today_speakers.add(q["speaker_ja"])
        if dry_run:
            logger.info(f"[dry-run] {hhmm} {q['id']} {q['speaker_en']}: {q['text_en']}")
            done.append(entry)
            continue
        _save(sched_path, sched)
        try:
            video = render(q["id"], ROOT / "output" / "kotonex" / day)
            meta = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))
            posts = build_posts(meta, q.get("kind") == "anime")
            entry["job"] = enqueue(video, posts, at, {"quote_id": q["id"], "source": "quote-clip-pipeline"})
            entry["title"] = posts["title"]
        except Exception as e:  # keep going; the next hourly run retries this slot
            logger.error(f"{hhmm} {q['id']} failed: {e}")
            entry["error"] = str(e)[:300]
            _save(sched_path, sched)
            continue
        entry.pop("error", None)
        used.add(q["id"])
        _save(sched_path, sched)
        _save(used_path, sorted(used))
        logger.info(f"queued {hhmm} {q['id']} → job {entry['job']}")
        done.append(entry)
    return done


def main() -> None:
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description="Render + queue today's KOTONEX shorts")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--now", action="store_true", help="queue one extra post due immediately")
    p.add_argument("--quotes-data", default=os.getenv("QUOTES_DATA", "~/kotodama-codex/data"))
    a = p.parse_args()
    res = run(dt.datetime.now(JST), Path(a.quotes_data).expanduser(), dry_run=a.dry_run, immediate=a.now)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
