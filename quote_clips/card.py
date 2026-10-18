"""quote_clips/card.py — Quote text-card shorts (no third-party footage).

A card video shows one quote, who said it and a text citation on the app's own
background (same look as the KOTODAMA share image), then reuses render.compose
for the app-name end card, CC BY BGM and loudness normalization.

Layout follows the app's design system (meigen-app docs/design-system.md §6/§8,
IBM Carbon g100): #161616 ground, IBM Plex Sans JP Light quote, SemiBold speaker,
Regular work/citation, no accent marks. The vertical position and reveal vary a
little per video so cards are not machine-identical.
"""

import json
import random
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger
from PIL import Image, ImageDraw, ImageFont

from quote_clips.llm import call_llm, parse_json
from quote_clips.render import FPS, H, W, compose, wrap_text

BG = (22, 22, 22)                       # Carbon g100 background
TEXT_PRIMARY = (244, 244, 244, 255)     # textPrimary
TEXT_SECONDARY = (198, 198, 198, 255)   # textSecondary
TEXT_TERTIARY = (168, 168, 168, 255)    # textHelper

MARGIN_X = 96
SAFE_TOP, SAFE_BOTTOM = 250, H - 250   # Stories / Reels UI bands stay empty
MAX_W = W - MARGIN_X * 2

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"   # IBM Plex Sans JP (OFL 1.1)
_FONTS = {
    "quote": ["IBMPlexSansJP-Light.ttf"],
    "speaker": ["IBMPlexSansJP-SemiBold.ttf"],
    "work": ["IBMPlexSansJP-Regular.ttf"],
    "meta": ["IBMPlexSansJP-Regular.ttf"],
    "meta_cjk": ["IBMPlexSansJP-Regular.ttf"],
}
# Reading speed (characters per second) used to size the hold time.
_READ_RATE = {"ja": 7.0, "en": 16.0}


def _font(role: str, size: int) -> ImageFont.FreeTypeFont:
    for name in _FONTS[role]:
        path = _FONT_DIR / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    from quote_clips.render import _font as fallback  # QUOTE_FONT / Noto on other systems
    return fallback(size)


# ── Text ─────────────────────────────────────────────────────────────────

BREAK_PROMPT = """Insert line breaks into this quote for a vertical video card.
Language: {lang}. Each visual line at most {max_chars} characters.
{rule}
Do NOT change, add or remove any character other than inserting "\\n".
Output JSON only: {{"text": "..."}}

Quote:
{text}"""

_BREAK_RULE = {
    "ja": "改行は文節・意味の切れ目（助詞や読点の直後など）だけ。単語や複合語の途中では切らない。",
    "en": "Break only between natural phrases (after a comma, before a conjunction/preposition). Never split a word or name.",
}
_MAX_CHARS = {"ja": 14, "en": 26}


def break_lines(text: str, lang: str) -> str:
    """LLM phrase-boundary line breaks; falls back to the renderer's wrap when the model edits the text."""
    text = text.strip()
    try:
        parsed = parse_json(call_llm(BREAK_PROMPT.format(
            lang=lang, max_chars=_MAX_CHARS[lang], rule=_BREAK_RULE[lang], text=text), temperature=0.0))
    except Exception as e:
        logger.warning(f"Card line-break pass failed: {e}")
        parsed = None
    if isinstance(parsed, dict):
        broken = str(parsed.get("text", "")).replace("\\n", "\n").strip()
        if _same_text(broken, text):
            return broken
        logger.warning("Card line-break pass changed the wording; using rule-based wrap")
    return text


def _same_text(a: str, b: str) -> bool:
    squash = lambda s: "".join(s.split())  # noqa: E731
    return bool(a) and squash(a) == squash(b)


def _fit_lines(text: str, role: str, size: int, min_size: int, max_lines: int) -> tuple:
    """Lines + font for left-aligned text inside MAX_W (explicit "\\n" kept, shrink before wrapping)."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()] or [""]
    measure = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    font = _font(role, size)
    while font.size > min_size and (
        max(measure.textlength(p, font=font) for p in paragraphs) > MAX_W or len(paragraphs) > max_lines
    ):
        font = _font(role, font.size - 4)
    lines: List[str] = []
    for p in paragraphs:
        lines += [p] if measure.textlength(p, font=font) <= MAX_W else wrap_text(p, font, MAX_W)
    return lines, font


# ── Layout ───────────────────────────────────────────────────────────────


def card_layout(quote: str, speaker: str, work: str, cite: str, lang: str, seed: str) -> Dict:
    """Positions of every text line, decided once so the PNG layers line up."""
    rng = random.Random(seed)
    q_size = 84 if lang == "ja" else 76   # Carbon quotation02 scaled for 1080px video
    if len(quote.replace("\n", "")) > (60 if lang == "ja" else 140):
        q_size -= 16
    q_lines, q_font = _fit_lines(quote, "quote", q_size, 40, 8)
    s_lines, s_font = _fit_lines(speaker, "speaker", 48, 36, 2)
    w_font = _font("work", 40)
    cite = _strip_work(cite, work)
    meta_role = "meta" if cite.isascii() else "meta_cjk"  # Menlo has no Japanese glyphs
    m_lines, m_font = _fit_lines(cite, meta_role, 34, 26, 2) if cite else ([], _font("meta", 34))

    def line_h(font: ImageFont.FreeTypeFont, spacing: float) -> int:
        ascent, descent = font.getmetrics()
        return int((ascent + descent) * spacing)

    blocks = []  # (kind, lines, font, color, line_height, gap_after)
    blocks.append(("quote", q_lines, q_font, TEXT_PRIMARY, line_h(q_font, 1.25), 96))
    blocks.append(("speaker", s_lines, s_font, TEXT_PRIMARY, line_h(s_font, 1.3), 12))
    if work:
        blocks.append(("work", [work], w_font, TEXT_SECONDARY, line_h(w_font, 1.2), 14))
    if m_lines:
        blocks.append(("meta", m_lines, m_font, TEXT_TERTIARY, line_h(m_font, 1.25), 0))

    total = sum(len(b[1]) * b[4] if b[0] != "rule" else b[4] for b in blocks) + sum(b[5] for b in blocks)
    room = SAFE_BOTTOM - SAFE_TOP - total
    # Optical center slightly above the middle, nudged per video.
    y = SAFE_TOP + max(0, int(room * 0.48) + rng.randint(-60, 60))
    placed = []
    for kind, lines, font, color, lh, gap in blocks:
        if kind == "rule":
            placed.append({"kind": kind, "y": y, "h": lh})
            y += lh + gap
            continue
        rows = []
        for ln in lines:
            rows.append({"text": ln, "y": y})
            y += lh
        placed.append({"kind": kind, "rows": rows, "font": font, "color": color})
        y += gap
    return {
        "blocks": placed,
        "bg": BG,
        "reveal_by_line": rng.random() < 0.7,
        "quote_chars": len(quote.replace("\n", "")),
    }


def _strip_work(cite: str, work: str) -> str:
    """Drop a leading work title from the citation; the work already has its own line."""
    cite = cite.strip()
    if work and cite.startswith(work):
        cite = cite[len(work):].lstrip(" ,、·:：")
    return cite


def _layer(path: Path, draw_fn) -> str:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(img))
    img.save(path)
    return str(path)


def card_timeline(layout: Dict, lang: str) -> tuple:
    """(layers, duration): each layer is (kind, rows-or-None, appear_at)."""
    quote_rows = layout["blocks"][0]["rows"]
    t = 0.5
    appear = []
    if layout["reveal_by_line"]:
        for i in range(len(quote_rows)):
            appear.append(("quote_row", i, t))
            t += 0.35
    else:
        appear.append(("quote_all", None, t))
        t += 0.6
    speaker_at = t + 0.4
    appear.append(("rest", None, speaker_at))
    read = layout["quote_chars"] / _READ_RATE.get(lang, 7.0)
    duration = max(6.0, min(14.0, speaker_at + read + 2.5))
    return appear, round(duration, 2)


def render_card_base(layout: Dict, lang: str, out_path: str) -> float:
    """Silent 1080x1920 card animation (text fades in). Returns its duration."""
    parts = Path(out_path + ".layers")
    parts.mkdir(parents=True, exist_ok=True)
    appear, duration = card_timeline(layout, lang)
    blocks = layout["blocks"]
    quote = blocks[0]

    bg = Image.new("RGBA", (W, H), (*layout["bg"], 255))
    bg_path = str(parts / "bg.png")
    bg.save(bg_path)

    def draw_rows(block: Dict, rows: List[Dict]):
        def fn(draw: ImageDraw.ImageDraw) -> None:
            for r in rows:
                draw.text((MARGIN_X, r["y"]), r["text"], font=block["font"], fill=block["color"])
        return fn

    def draw_rest(draw: ImageDraw.ImageDraw) -> None:
        for b in blocks[1:]:
            for r in b["rows"]:
                draw.text((MARGIN_X, r["y"]), r["text"], font=b["font"], fill=b["color"])

    layers = []  # (png, appear_at)
    for k, (kind, idx, at) in enumerate(appear):
        path = parts / f"layer_{k:02d}.png"
        if kind == "quote_row":
            layers.append((_layer(path, draw_rows(quote, [quote["rows"][idx]])), at))
        elif kind == "quote_all":
            layers.append((_layer(path, draw_rows(quote, quote["rows"])), at))
        else:
            layers.append((_layer(path, draw_rest), at))

    inputs = ["-loop", "1", "-framerate", str(FPS), "-t", f"{duration}", "-i", bg_path]
    for png, _ in layers:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{duration}", "-i", png]
    inputs += ["-f", "lavfi", "-t", f"{duration}", "-i", "anullsrc=r=44100:cl=stereo"]
    filters = ["[0:v]format=rgba[b0]"]
    last = "[b0]"
    for k, (_, at) in enumerate(layers, start=1):
        filters.append(f"[{k}:v]format=rgba,fade=t=in:st={at:.2f}:d=0.3:alpha=1[l{k}]")
        filters.append(f"{last}[l{k}]overlay=0:0[b{k}]")
        last = f"[b{k}]"
    filters.append(f"{last}format=yuv420p,setsar=1[v]")
    audio_idx = len(layers) + 1
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters), "-map", "[v]", "-map", f"{audio_idx}:a",
           "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-shortest", out_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg card failed: {e.stderr[-800:]}") from e
    finally:
        for p in parts.glob("*.png"):
            p.unlink()
        parts.rmdir()
    return duration


def make_card_video(quote: str, speaker: str, work: str, cite: str, lang: str, out_path: str, seed: str,
                    app_name: str, music_dir: str, use_bgm: bool, tagline: str = "",
                    line_break: bool = True) -> Optional[str]:
    """Full card short (card → end card, BGM, loudnorm). Returns the BGM file used."""
    text = break_lines(quote, lang) if line_break else quote
    layout = card_layout(text, speaker, work, cite, lang, seed)
    base = out_path + ".base.mp4"
    try:
        render_card_base(layout, lang, base)
        return compose(base, [], hook="", telop="", app_name=app_name, out_path=out_path,
                       music_dir=music_dir, use_bgm=use_bgm, tagline=tagline)
    finally:
        Path(base).unlink(missing_ok=True)


# ── Quote data shared with the app ───────────────────────────────────────


def load_app_quotes(data_dir: str) -> Dict[str, Dict]:
    """Quotes from the app's data folder (quotes_*.json + works.json), keyed by id.

    Lets a sources.csv card row say only `quote_id`, so SNS shows exactly the
    words, speaker and citation the app shows.
    """
    root = Path(data_dir).expanduser()
    if not root.is_dir():
        return {}
    works = {}
    works_file = root / "works.json"
    if works_file.exists():
        works = {w["id"]: w for w in json.loads(works_file.read_text(encoding="utf-8"))}
    quotes: Dict[str, Dict] = {}
    for f in sorted(root.glob("quotes_*.json")):
        for q in json.loads(f.read_text(encoding="utf-8")):
            work = works.get(q.get("work_id") or "", {})
            q["work_ja"] = work.get("ja", "")
            q["work_en"] = work.get("en", "")
            quotes[q["id"]] = q
    return quotes
