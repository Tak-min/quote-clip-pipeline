"""quote_clips/render.py — Cut, lay out vertically, overlay Japanese text, append end card.

Remixed from clip_pipeline/cutter.py. Differences:
- Frame-accurate cut + vertical layout in one ffmpeg pass (the original cut with
  stream copy and ±0.5s padding, which shifted captions).
- "fit" layout: whole 16:9 frame centered on a blurred background (faces never cropped).
- Phrase subtitles in Japanese (original or translated) rendered with a CJK font.
- Speaker telop, top hook line, and a short app-name end card.
- Text overlays, end card, BGM and loudnorm composited in one ffmpeg pass (no moviepy).
"""

import os
import random
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
BGM_VOLUME = 0.08
END_CARD_SEC = 1.5

SUB_GAP = 30             # gap between picture bottom and subtitles

_FONT_CANDIDATES = [
    os.getenv("QUOTE_FONT", ""),
    "/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]
_NO_LINE_START = set("、。，．・）」』】！？!?,.ー〜ぁぃぅぇぉっゃゅょァィゥェォッャュョ")


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size)
    raise RuntimeError("No CJK font found. Set QUOTE_FONT to a .ttc/.ttf with Japanese glyphs.")


# ── Text layout ──────────────────────────────────────────────────────────


def _tokens(text: str) -> List[str]:
    """Split into wrap units: latin words stay whole, CJK breaks per character."""
    out: List[str] = []
    buf = ""
    for ch in text:
        if ch.isascii() and not ch.isspace():
            buf += ch
            continue
        if buf:
            out.append(buf)
            buf = ""
        out.append(ch)
    if buf:
        out.append(buf)
    return out


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> List[str]:
    """Fallback wrap for a line that is still too wide after shrinking.

    Breaks at the last natural point (after punctuation / particle / space) that keeps the
    line within max_w; only breaks between arbitrary characters when no such point exists.
    """
    measure = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    toks = _tokens(text.strip())
    lines: List[str] = []
    cur: List[str] = []
    for tok in toks:
        if cur and measure.textlength("".join(cur) + tok, font=font) > max_w and tok not in _NO_LINE_START:
            cut = _last_break(cur)
            if cut is None or cut < len(cur) * 0.4:
                cut = len(cur)
            lines.append("".join(cur[:cut]).strip())
            cur = cur[cut:]
            while cur and cur[0] == " ":
                cur.pop(0)
        cur.append(tok)
    if "".join(cur).strip():
        lines.append("".join(cur).strip())
    return lines


_BREAK_AFTER = set("、。，．！？!?」』）・ ,") | set("はがをにでともへてねよ")


def _last_break(tokens: List[str]) -> Optional[int]:
    """Index just after the last token that is a natural line-break point."""
    for i in range(len(tokens) - 1, 0, -1):
        if tokens[i - 1] in _BREAK_AFTER or tokens[i] == " ":
            return i
    return None


def layout_lines(text: str, size: int, max_w: int, max_lines: int, min_size: int = 40) -> tuple:
    """Lines to draw + font. Explicit "\n" breaks (from Gemini) are kept; shrink before wrapping."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()] or [""]
    measure = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    font = _font(size)
    while font.size > min_size and (
        max(measure.textlength(p, font=font) for p in paragraphs) > max_w or len(paragraphs) > max_lines
    ):
        font = _font(font.size - 4)
    lines: List[str] = []
    for p in paragraphs:
        lines += [p] if measure.textlength(p, font=font) <= max_w else wrap_text(p, font, max_w)
    return lines, font


def text_image(
    text: str,
    size: int,
    max_w: int = int(W * 0.88),
    max_lines: int = 3,
    color: tuple = (255, 255, 255, 255),
    plate: Optional[tuple] = (0, 0, 0, 170),
    stroke: int = 6,
) -> Image.Image:
    """Render text on an optional rounded plate, honoring explicit line breaks."""
    lines, font = layout_lines(text, size, max_w, max_lines)

    measure = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    gap = int(line_h * 0.15)
    pad_x, pad_y = 36, 24
    widest = max((measure.textlength(ln, font=font) for ln in lines), default=1)
    cw = int(widest + pad_x * 2 + stroke * 2)
    ch = int(len(lines) * line_h + max(0, len(lines) - 1) * gap + pad_y * 2 + stroke * 2)

    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if plate:
        draw.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=28, fill=plate)
    y = pad_y + stroke
    for ln in lines:
        lw = measure.textlength(ln, font=font)
        draw.text(((cw - lw) / 2, y), ln, font=font, fill=color, stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        y += line_h + gap
    return img


# ── Video steps ──────────────────────────────────────────────────────────


def _source_size(src: str) -> tuple:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", src]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    w, h = (int(x) for x in out.split(",")[:2])
    return w, h


def prepare_base(src: str, start: float, end: float, dst: str, layout: str = "fit") -> tuple:
    """Frame-accurate cut + 1080x1920 layout in a single re-encode.

    Returns the (top, bottom) y-range the picture occupies, for placing text around it.
    """
    if layout == "crop":
        vf = f"crop=ih*9/16:ih,scale={W}:{H}:flags=lanczos,setsar=1"
    else:
        vf = (
            f"split[a][b];"
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2,eq=brightness=-0.28[bg];"
            f"[b]scale={W}:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    cmd = [
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", src, "-t", f"{end - start:.3f}",
        "-filter_complex" if layout != "crop" else "-vf", vf,
        "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", dst,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    if layout == "crop":
        return 0, H
    sw, sh = _source_size(src)
    pic_h = min(H, int(round(W * sh / sw / 2) * 2))
    top = (H - pic_h) // 2
    return top, top + pic_h


def text_positions(picture: tuple) -> Dict[str, int]:
    """Hook + speaker in the band above the picture, subtitles in the band below.

    Falls back to fixed positions over the picture when it fills the frame.
    """
    top, bottom = picture
    if top < 300:  # vertical/cropped source: text sits on the footage
        return {"hook_center": 300, "telop_top": 420, "sub_top": 1300}
    return {"hook_center": int(top * 0.38), "telop_top": top - 130, "sub_top": bottom + SUB_GAP}


def _pick_bgm(music_dir: str, seed: str) -> Optional[str]:
    tracks = sorted(p for p in Path(music_dir).glob("*.mp3"))
    return str(random.Random(seed).choice(tracks)) if tracks else None


def _frame_png(path: str, layers: List[tuple], bg: Optional[tuple] = None) -> None:
    """Paste (image, y, centered) layers onto a full 1080x1920 canvas and save as PNG."""
    canvas = Image.new("RGBA", (W, H), bg or (0, 0, 0, 0))
    for img, y, center in layers:
        top = int(y - img.height / 2) if center else int(y)
        canvas.alpha_composite(img, ((W - img.width) // 2, max(0, top)))
    canvas.save(path)


def compose(
    base_path: str,
    subtitles: List[Dict],
    hook: str,
    telop: str,
    app_name: str,
    out_path: str,
    picture: tuple = (656, 1264),
    music_dir: str = "assets/music",
    use_bgm: bool = True,
    tagline: str = "",
) -> Optional[str]:
    """Overlay texts, append the end card, mix BGM and normalize loudness in ONE ffmpeg pass.

    Returns the BGM file used (None if no BGM) so the caller can credit it.

    Text is pre-rendered to full-frame PNGs with PIL (CJK font), then ffmpeg overlays each
    PNG during its time window. Much faster than per-frame Python compositing.
    """
    dur = _duration(base_path)
    pos = text_positions(picture)
    tmp_dir = Path(out_path + ".parts")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Static layer (hook + speaker) for the whole clip, then one PNG per subtitle line.
    pngs: List[tuple] = []  # (path, start, end)
    static_layers = []
    if hook:
        static_layers.append((text_image(hook, 76, max_lines=2, color=(255, 214, 10, 255)), pos["hook_center"], True))
    if telop:
        static_layers.append((text_image(telop, 46, plate=(0, 0, 0, 120), stroke=4), pos["telop_top"], False))
    if static_layers:
        _frame_png(str(tmp_dir / "static.png"), static_layers)
        pngs.append((str(tmp_dir / "static.png"), 0.0, dur))
    for i, sub in enumerate(subtitles):
        s_start = max(0.0, sub["start"])
        s_end = subtitles[i + 1]["start"] if i + 1 < len(subtitles) else max(sub["end"], dur)
        s_end = min(s_end, dur)
        if s_end - s_start < 0.2 or not sub["text"].strip():
            continue
        path = str(tmp_dir / f"sub_{i:03d}.png")
        _frame_png(path, [(text_image(sub["text"], 64), pos["sub_top"], False)])
        pngs.append((path, s_start, s_end))

    inputs = ["-i", base_path]
    for path, _, _ in pngs:
        inputs += ["-i", path]
    n = len(pngs)
    filters = []
    last = "[0:v]"
    for k, (_, a, b) in enumerate(pngs, start=1):
        filters.append(f"{last}[{k}:v]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})'[v{k}]")
        last = f"[v{k}]"
    filters.append(f"{last}format=yuv420p,setsar=1[vmain]")
    filters.append("[0:a]aformat=sample_rates=44100:channel_layouts=stereo[amain]")
    v_out, a_out, total = "[vmain]", "[amain]", dur

    if app_name:
        card = str(tmp_dir / "endcard.png")
        layers = [(text_image(app_name, 120, plate=None, stroke=0), H // 2 - 40, True)]
        if tagline:
            layers.append((text_image(tagline, 52, plate=None, stroke=0, color=(200, 200, 210, 255)), H // 2 + 110, True))
        _frame_png(card, layers, bg=(10, 10, 14, 255))
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{END_CARD_SEC}", "-i", card]
        k = n + 1
        filters.append(f"[{k}:v]format=yuv420p,setsar=1[vcard]")
        filters.append(f"anullsrc=r=44100:cl=stereo,atrim=0:{END_CARD_SEC}[acard]")
        filters.append("[vmain][amain][vcard][acard]concat=n=2:v=1:a=1[vcat][acat]")
        v_out, a_out, total = "[vcat]", "[acat]", dur + END_CARD_SEC
        n += 1

    track = _pick_bgm(music_dir, seed=out_path) if use_bgm else None
    if track:
        inputs += ["-i", track]
        k = n + 1
        filters.append(
            f"[{k}:a]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{total:.3f},"
            f"volume={BGM_VOLUME},afade=t=in:d=0.3,afade=t=out:st={max(0.0, total - 0.6):.3f}:d=0.6[bgm]"
        )
        filters.append(f"{a_out}[bgm]amix=inputs=2:duration=first:normalize=0[amix]")
        a_out = "[amix]"
    filters.append(f"{a_out}loudnorm=I=-14:TP=-1.5:LRA=11[aout]")

    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters), "-map", v_out, "-map", "[aout]",
           "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-movflags", "+faststart", out_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg compose failed: {e.stderr[-800:]}") from e
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return track


def _duration(path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]
    return float(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip())
