"""quote_clips/footage.py — licence-clean "scene" footage for quote shorts.

The headline layout (headline above, 16:9 scene with subtitles, context/lesson below) needs a
picture. research/copyright/DECISION.md forbids anime/film/TV footage, so the scene is built
only from material we may reuse commercially and edit:
  - the speaker's public-domain portrait / PD archival clip shipped with the app
    (kotodama-codex Resources/Portraits, Resources/Clips; licences in data/portraits.json,
    data/figure_clips.json), and
  - stock video from Pexels (Pexels License) or Pixabay (Content License), both free for
    commercial use with modification, no attribution required (we credit anyway).

One shot per subtitle card, like the reference shorts that cut on every line.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from dotenv import dotenv_values

from quote_clips.llm import DEFAULT_SHARED_ENV, _config, call_llm, parse_json
from quote_clips.render import BOXES, FPS, H, W

UA = {"User-Agent": "quote-clip-pipeline/1.0"}
MIN_SHOT_SEC = 2.0

# Fallback search terms per app scene tag when the LLM is unavailable.
SCENE_QUERIES = {
    "monday": ["sunrise over city", "morning coffee window", "commuters walking morning"],
    "big_moment": ["stadium lights night", "athlete preparing", "stage spotlight"],
    "slacking": ["night desk lamp", "rain on window night", "city lights at night"],
    "setback": ["storm clouds timelapse", "lonely road rain", "waves crashing rocks"],
    "training": ["runner at dawn", "boxing training gym", "athlete stretching"],
    "study": ["student writing notes", "library books", "desk lamp studying"],
}
GENERIC = ["mountain sunrise", "person walking forward road", "ocean horizon sunset", "city skyline timelapse"]

QUERY_PROMPT = """You choose stock-video search queries for a vertical quote short.
Quote: "{quote}"
Speaker: {speaker}{work}
Context: {scene}

Return {n} different English search queries (2-4 words each) for Pexels stock video that
visually match the mood and story of the quote, as cinematic real-life shots.
Rules: no celebrities or real names, no anime, no logos, no text on screen, no brand names.
Vary the shots (wide landscape, close-up of hands/feet, a person from behind, nature, city).
Return JSON only: ["query 1", "query 2", ...]"""


def _key(name: str) -> str:
    """Env / LLM_ENV_FILE first, then the shared key file (on the Pi LLM_ENV_FILE points at this repo's .env)."""
    value = _config().get(name, "")
    if not value and DEFAULT_SHARED_ENV.is_file():
        value = dotenv_values(DEFAULT_SHARED_ENV).get(name) or ""
    return value


def _get_json(url: str, headers: Optional[Dict] = None) -> Dict:
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def scene_queries(quote: str, speaker: str, work: str, scene: str, tags: List[str], n: int) -> List[str]:
    """Search queries from the LLM, padded with the scene-tag fallbacks."""
    out: List[str] = []
    try:
        data = parse_json(call_llm(QUERY_PROMPT.format(
            quote=quote, speaker=speaker or "unknown", work=f" ({work})" if work else "", scene=scene or "-", n=n)))
        if isinstance(data, list):
            out = [re.sub(r"[^\w\s-]", "", str(q)).strip() for q in data if str(q).strip()]
    except Exception as e:
        logger.warning(f"scene query LLM failed: {e}")
    for t in tags:
        out += SCENE_QUERIES.get(t, [])
    out += GENERIC
    seen, uniq = set(), []
    for q in out:
        if q and q.lower() not in seen:
            seen.add(q.lower())
            uniq.append(q)
    return uniq


def _best_file(files: List[Dict]) -> Optional[Dict]:
    """Landscape file closest to 1920 wide (enough for a 1040px box, small download)."""
    ok = [f for f in files if f.get("width") and f.get("height") and f["width"] > f["height"] and f["width"] >= 1280]
    return min(ok, key=lambda f: abs(f["width"] - 1920)) if ok else None


def search_pexels(query: str, per_page: int = 8) -> List[Dict]:
    key = _key("PEXELS_API_KEY")
    if not key:
        return []
    q = urllib.parse.urlencode({"query": query, "orientation": "landscape", "size": "medium", "per_page": per_page})
    data = _get_json(f"https://api.pexels.com/videos/search?{q}", {"Authorization": key})
    out = []
    for v in data.get("videos", []):
        f = _best_file(v.get("video_files", []))
        if f and v.get("duration", 0) >= 4:
            out.append({"id": f"pexels-{v['id']}", "file": f["link"], "duration": v["duration"],
                        "credit": f"{v.get('user', {}).get('name', 'Pexels')} (Pexels)", "page": v.get("url", "")})
    return out


def search_pixabay(query: str, per_page: int = 8) -> List[Dict]:
    key = _key("PIXABAY_API_KEY")
    if not key:
        return []
    q = urllib.parse.urlencode({"key": key, "q": query, "per_page": per_page, "safesearch": "true"})
    data = _get_json(f"https://pixabay.com/api/videos/?{q}")
    out = []
    for v in data.get("hits", []):
        vids = v.get("videos", {})
        f = next((vids[k] for k in ("large", "medium") if vids.get(k, {}).get("url") and vids[k].get("width", 0) >= 1280), None)
        if f and v.get("duration", 0) >= 4:
            out.append({"id": f"pixabay-{v['id']}", "file": f["url"], "duration": v["duration"],
                        "credit": f"{v.get('user', 'Pixabay')} (Pixabay)", "page": v.get("pageURL", "")})
    return out


def _download(url: str, dst: Path) -> Path:
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    tmp = dst.with_suffix(".part")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(dst)
    return dst


def stock_shots(queries: List[str], n: int, cache_dir: Path, seed: str) -> List[Dict]:
    """n distinct stock clips, one per query where possible; avoids clips used by earlier shorts."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    used_path = cache_dir / "used.json"
    used = set(json.loads(used_path.read_text())) if used_path.exists() else set()
    rng = random.Random(seed)
    shots: List[Dict] = []
    taken = set()
    for query in queries:
        if len(shots) >= n:
            break
        hits: List[Dict] = []
        for search in (search_pexels, search_pixabay):
            try:
                hits = search(query)
            except Exception as e:
                logger.warning(f"{search.__name__}({query!r}) failed: {e}")
            hits = [h for h in hits if h["id"] not in used and h["id"] not in taken]
            if hits:
                break
        if not hits:
            continue
        hit = rng.choice(hits[:4])
        try:
            hit["path"] = str(_download(hit["file"], cache_dir / f"{hit['id']}.mp4"))
        except Exception as e:
            logger.warning(f"download failed {hit['id']}: {e}")
            continue
        hit["query"] = query
        taken.add(hit["id"])
        shots.append(hit)
    used_path.write_text(json.dumps(sorted(used | taken)))
    return shots


def app_media(quote: Dict, resources: Path, data_dir: Path) -> Optional[Dict]:
    """The speaker's PD archival video or portrait from the app, with its licence."""
    clips = _read(data_dir / "figure_clips.json")
    c = clips.get(quote.get("id", ""))
    if c and c.get("video") and _reusable(c.get("license", "")) and (resources / "Clips" / c["file"]).exists():
        return {"kind": "video", "path": str(resources / "Clips" / c["file"]),
                "credit": f"{c.get('credit', '')} ({c.get('license', '')}, Wikimedia Commons)"}
    p = _read(data_dir / "portraits.json").get(quote.get("speaker_en", ""))
    if p and not _reusable(p.get("license", "")):
        p = None  # share-alike / non-commercial licences would bind the whole video
    if p:
        path = next(iter(sorted((resources / "Portraits").glob(f"{p['file']}.*"))), None)
        if path:
            return {"kind": "image", "path": str(path),
                    "credit": f"Portrait: {p.get('artist', '')} ({p.get('license', '')}, Wikimedia Commons)"}
    return None


def _reusable(license_name: str) -> bool:
    lic = license_name.upper()
    return lic in ("PUBLIC DOMAIN", "CC0") or (lic.startswith("CC BY ") and not any(x in lic for x in ("SA", "NC", "ND")))


def _read(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _probe_duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out or 0)


def build_base(shots: List[Dict], durations: List[float], dst: str, style: str = "headline") -> tuple:
    """Cut shots to the subtitle timings and lay them in the headline box on black, with silent audio.

    Returns the (top, bottom) y-range of the box, like render.prepare_base.
    """
    x, y, bw, box_h = BOXES[style]
    bh = box_h // 2 * 2  # yuv420p needs even sizes; the 1px under the box is hidden by the mask
    inputs: List[str] = []
    parts: List[str] = []
    for i, (shot, d) in enumerate(zip(shots, durations)):
        d = max(d, 0.5)
        if shot.get("kind") == "image":
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", shot["path"]]
            # portrait: fit inside the box over a blurred copy, slow push-in
            frames = int(d * FPS)
            parts.append(
                f"[{i}:v]split[p{i}a][p{i}b];"
                f"[p{i}a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=30:2,"
                f"eq=brightness=-0.25[p{i}bg];"
                f"[p{i}b]scale=-2:{bh}[p{i}fg];[p{i}bg][p{i}fg]overlay=(W-w)/2:0,"
                f"scale={bw * 2}:{bh * 2},zoompan=z='min(1+0.0009*on,1.12)':x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)':d=1:s={bw}x{bh}:fps={FPS},trim=duration={d:.3f},setpts=PTS-STARTPTS,fps={FPS},format=yuv420p,"
                f"setsar=1[s{i}]")
            continue
        src_d = shot.get("src_duration") or _probe_duration(shot["path"])
        start = max(0.0, min(src_d - d, src_d * 0.25)) if src_d > d else 0.0
        inputs += ["-ss", f"{start:.3f}", "-i", shot["path"]]
        loop = "" if src_d >= d + start else f",loop=-1:size={int(src_d * FPS)}"
        parts.append(
            f"[{i}:v]fps={FPS}{loop},scale={bw}:{bh}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={bw}:{bh},trim=duration={d:.3f},setpts=PTS-STARTPTS,format=yuv420p,setsar=1[s{i}]")
    n = len(shots)
    total = sum(max(d, 0.5) for d in durations)
    graph = ";".join(parts) + ";" + "".join(f"[s{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[box];" \
        f"color=black:s={W}x{H}:r={FPS}:d={total:.3f}[bg];[bg][box]overlay={x}:{y}:shortest=1,format=yuv420p[v]"
    cmd = ["ffmpeg", "-y", *inputs, "-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=44100:cl=stereo",
           "-filter_complex", graph, "-map", "[v]", "-map", f"{n}:a", "-t", f"{total:.3f}",
           "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "128k", dst]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        hits = [ln for ln in e.stderr.splitlines() if "match" in ln or "Error" in ln][:3]
        raise RuntimeError(f"ffmpeg scene base failed: {hits or e.stderr[-800:]}") from e
    return y, y + box_h


# ── subtitles for a text quote (no audio to time against) ────────────────

MAX_CARD_CHARS = 34


_WEAK_END = {"a", "an", "the", "to", "of", "in", "on", "at", "for", "with", "by", "from", "and", "or", "but",
             "is", "are", "be", "was", "were", "you", "i", "my", "your", "his", "her", "their", "our", "its",
             "that", "which", "who", "where", "when", "if", "as", "not", "no", "so", "than", "thou", "thy", "we"}
_STRONG_START = {"and", "but", "or", "so", "when", "while", "because", "if", "that", "which", "who", "where",
                 "unless", "until", "though", "although", "before", "after", "as"}


def _break_cost(last: str, nxt: str) -> float:
    """Cost of ending a card after `last` when `nxt` starts the next one (lower = more natural)."""
    if last[-1] in ".!?":
        return 0.0
    if last[-1] in ",;:\u2014":
        return 1.0
    word = re.sub(r"[^\w']", "", last).lower()
    if word in _WEAK_END:
        return 9.0
    if re.sub(r"[^\w']", "", nxt).lower() in _STRONG_START:
        return 2.5
    return 5.0


def quote_cards(text: str, max_chars: int = MAX_CARD_CHARS) -> List[str]:
    """Split a quote into one-line subtitle cards at the most natural points (punctuation,
    before conjunctions, never after an article/preposition), never mid-word."""
    text = re.sub(r",?\s*--+\s*", " \u2014 ", text.replace("\n", " "))
    words = text.split()
    # glue a lone dash to the previous word so it never starts a card
    glued: List[str] = []
    for w in words:
        if w == "\u2014" and glued:
            glued[-1] += " \u2014"
        else:
            glued.append(w)
    words = glued
    n = len(words)
    if not n:
        return []
    best = [0.0] + [float("inf")] * n   # best[i] = cost of splitting words[:i]
    prev = [0] * (n + 1)
    for i in range(1, n + 1):
        for j in range(i - 1, -1, -1):
            card = " ".join(words[j:i])
            if len(card) > max_chars and i - j > 1:
                break
            end = 0.0 if i == n else _break_cost(words[i - 1], words[i])
            short = max(0, max_chars * 0.4 - len(card)) * 0.4    # avoid tiny orphan cards
            cost = best[j] + 4.0 + end + short
            if cost < best[i]:
                best[i], prev[i] = cost, j
    cards, i = [], n
    while i > 0:
        cards.append(" ".join(words[prev[i]:i]))
        i = prev[i]
    return cards[::-1]


def card_timings(cards: List[str], lead: float = 0.4, tail: float = 1.6) -> List[Dict]:
    """Reading-speed timings (~2.6 words/s, at least MIN_SHOT_SEC each); the last card holds longer."""
    subs, t = [], lead
    for i, c in enumerate(cards):
        d = max(MIN_SHOT_SEC + 0.4, len(c.split()) / 2.6 + 0.9)
        if i == len(cards) - 1:
            d += tail
        subs.append({"text": c, "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    subs[0]["start"] = 0.0
    return subs


def spread_cards(text: str, t0: float, t1: float, total: float) -> List[Dict]:
    """Subtitle cards for a known line, spread over the time it is spoken (t0..t1), sized by length."""
    cards = quote_cards(text)
    t0, t1 = max(0.0, t0), max(t0 + 0.8 * len(cards), t1)
    weights = [len(c) + 8 for c in cards]
    subs, t = [], t0
    for c, w in zip(cards, weights):
        d = (t1 - t0) * w / sum(weights)
        subs.append({"text": c, "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    subs[-1]["end"] = round(max(subs[-1]["end"], total), 3)
    return subs
