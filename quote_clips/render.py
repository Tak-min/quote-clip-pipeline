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
NOTIFY_SEC = 3.5         # app-notification banner shown over the last seconds (Shift "headline" style)

# Shift-style layouts: (x, y, w, h) of the footage box on a black 1080x1920 frame.
#   headline: bold 2-line headline above, rounded 16:9 box, white/yellow commentary below,
#             subtitles on the footage, app-notification banner at the end (@shiftjp.app)
#   plain:    footage band in the middle, thin mincho subtitles only (@kachimind_jp)
BOXES = {"headline": (20, 600, 1040, 585), "plain": (0, 470, 1080, 940)}
BOX_RADIUS = 40
APP_ICON = Path(__file__).resolve().parent.parent / "assets" / "app_icon.png"

_FONT_CANDIDATES = [
    os.getenv("QUOTE_FONT", ""),
    "/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]
_NO_LINE_START = set("、。，．・）」』】！？!?,.ー〜ぁぃぅぇぉっゃゅょァィゥェォッャュョ")


MINCHO = "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc"


def _font(size: int, path: str = "") -> ImageFont.FreeTypeFont:
    if path and Path(path).exists():
        return ImageFont.truetype(path, size)
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


def layout_lines(text: str, size: int, max_w: int, max_lines: int, min_size: int = 40, font_path: str = "") -> tuple:
    """Lines to draw + font. Explicit "\n" breaks (from Gemini) are kept; shrink before wrapping."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()] or [""]
    measure = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    font = _font(size, font_path)
    while font.size > min_size and (
        max(measure.textlength(p, font=font) for p in paragraphs) > max_w or len(paragraphs) > max_lines
    ):
        font = _font(font.size - 4, font_path)
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
    font_path: str = "",
    min_size: int = 40,
    line_gap: float = 0.15,
    shadow: bool = False,
) -> Image.Image:
    """Render text on an optional rounded plate, honoring explicit line breaks."""
    lines, font = layout_lines(text, size, max_w, max_lines, min_size=min_size, font_path=font_path)

    measure = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    gap = int(line_h * line_gap)
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
        if shadow:  # soft drop shadow (Shift "plain" style subtitles)
            draw.text(((cw - lw) / 2 + 3, y + 3), ln, font=font, fill=(0, 0, 0, 200))
        draw.text(((cw - lw) / 2, y), ln, font=font, fill=color, stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        y += line_h + gap
    if shadow:
        from PIL import ImageFilter
        blurred = img.filter(ImageFilter.GaussianBlur(1.2))
        img = Image.alpha_composite(blurred, img)
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
    if layout in BOXES:  # Shift-style: footage in a box on black
        x, y, bw, bh = BOXES[layout]
        vf = (
            f"[0:v]scale={bw}:{bh}:force_original_aspect_ratio=increase:flags=lanczos,crop={bw}:{bh},setsar=1[fg];"
            f"color=black:s={W}x{H}:r={FPS}[bg];[bg][fg]overlay={x}:{y}:shortest=1,setsar=1"
        )
    elif layout == "crop":
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
    if layout in BOXES:
        _, y, _, bh = BOXES[layout]
        return y, y + bh
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


_ASSET_FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def _plain_text(text: str, font_file: str, size: int, color: tuple) -> Image.Image:
    """Single-line text image in a bundled font (falls back to the CJK system font)."""
    path = _ASSET_FONTS / font_file
    font = ImageFont.truetype(str(path), size) if path.exists() else _font(size)
    measure = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    left, top, right, bottom = measure.textbbox((0, 0), text, font=font)
    img = Image.new("RGBA", (right - left + 8, bottom - top + 8), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((4 - left, 4 - top), text, font=font, fill=color)
    return img


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


WHITE = (255, 255, 255, 255)
YELLOW = (255, 230, 0, 255)
GRAY = (225, 225, 225, 210)


def box_mask(layout: str) -> Image.Image:
    """Black frame with a transparent rounded hole over the footage box (rounded corners)."""
    x, y, bw, bh = BOXES[layout]
    radius = BOX_RADIUS if layout == "headline" else 0
    mask = Image.new("L", (W, H), 255)
    ImageDraw.Draw(mask).rounded_rectangle([x, y, x + bw - 1, y + bh - 1], radius=radius, fill=0)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    img.putalpha(mask)
    return img


def info_block(lines: List[tuple], size: int = 40, max_w: int = W - 80) -> Image.Image:
    """Centered multi-colour commentary under the footage: [(text, rgba), ...] one paragraph each."""
    imgs = [text_image(t, size, max_w=max_w, max_lines=3, color=c, plate=None, stroke=0, min_size=30,
                       line_gap=0.08) for t, c in lines if t.strip()]
    if not imgs:
        return Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    h = sum(i.height for i in imgs) - 40 * (len(imgs) - 1)   # paragraphs sit tight like the reference
    out = Image.new("RGBA", (max(i.width for i in imgs), max(h, 4)), (0, 0, 0, 0))
    y = 0
    for i in imgs:
        out.alpha_composite(i, ((out.width - i.width) // 2, y))
        y += i.height - 40
    return out


def watermark(app_name: str, lang: str = "ja") -> Image.Image:
    year = 2026
    text = f"Motivation app “{app_name}” © {year}" if lang == "en" else f"モチベーションアプリ「{app_name}」© {year}"
    return text_image(text, 22, plate=None, stroke=0, color=GRAY, min_size=18)


def notification(app_name: str, body: str, lang: str = "ja") -> Image.Image:
    """iOS-style push banner: app icon, app name, the quote as the notification body, 'now'."""
    bw, bh = 900, 150
    img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, bw - 1, bh - 1], radius=36, fill=(44, 44, 48, 240))
    if APP_ICON.exists():
        icon = Image.open(APP_ICON).convert("RGBA").resize((84, 84), Image.LANCZOS)
        m = Image.new("L", icon.size, 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, 83, 83], radius=20, fill=255)
        img.paste(icon, (28, (bh - 84) // 2), m)
    title_f, body_f = _font(32), _font(30, "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc")
    d.text((136, 30), app_name, font=title_f, fill=WHITE)
    now = "now" if lang == "en" else "今"
    d.text((bw - 36 - d.textlength(now, font=body_f), 32), now, font=body_f, fill=(170, 170, 170, 255))
    line = body.replace("\n", " ")
    while d.textlength(line, font=body_f) > bw - 170 and len(line) > 2:
        line = line[:-2] + "…"
    d.text((136, 78), line, font=body_f, fill=WHITE)
    return img


def end_card_layers(app_name: str, tagline: str, style: str, lang: str) -> tuple:
    """End card (layers, background). Shift styles: black, app icon + name; default: Carbon g100 name only."""
    if style in BOXES:
        layers = []
        if style == "headline":
            label = "Motivation app" if lang == "en" else "モチベーションアプリ"
            layers.append((_plain_text(label, "IBMPlexSansJP-Regular.ttf", 34, WHITE), H // 2 - 230, True))
            layers.append((_plain_text(f"「{app_name}」" if lang != "en" else app_name,
                                       "IBMPlexSansJP-SemiBold.ttf", 52, WHITE), H // 2 - 160, True))
        if APP_ICON.exists():
            icon = Image.open(APP_ICON).convert("RGBA").resize((220, 220), Image.LANCZOS)
            m = Image.new("L", icon.size, 0)
            ImageDraw.Draw(m).rounded_rectangle([0, 0, 219, 219], radius=50, fill=255)
            icon.putalpha(m)
            layers.append((icon, H // 2 + 20, True))
        if style == "plain" or not APP_ICON.exists():
            layers.append((_plain_text(app_name, "IBMPlexSansJP-Light.ttf", 64, WHITE), H // 2 + 200, True))
        return layers, (0, 0, 0, 255)
    layers = [(_plain_text(app_name, "IBMPlexSansJP-Light.ttf", 120, (244, 244, 244, 255)), H // 2 - 40, True)]
    if tagline:
        layers.append((_plain_text(tagline, "IBMPlexSansJP-Regular.ttf", 48, (198, 198, 198, 255)), H // 2 + 110, True))
    return layers, (22, 22, 22, 255)  # Carbon g100, matches the app (meigen-app docs/design-system.md)


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
    style: str = "",
    info: Optional[List[tuple]] = None,
    notify: str = "",
    lang: str = "ja",
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
    top, bottom = picture
    if style in BOXES:
        # Shift layout: rounded box mask, headline above, commentary below, watermark on the footage.
        static = box_mask(style)
        layers = [(watermark(app_name, lang), bottom - 34, True)] if app_name else []
        if style == "headline":
            if hook:
                layers.append((text_image(hook, 74, max_w=W - 60, max_lines=2, plate=None, stroke=0,
                                          line_gap=0.05), top - 130, True))
            if info:
                layers.append((info_block(info), bottom + 24, False))
        for img, y, center in layers:
            t = int(y - img.height / 2) if center else int(y)
            static.alpha_composite(img, ((W - img.width) // 2, max(0, t)))
        static.save(str(tmp_dir / "static.png"))
        pngs.append((str(tmp_dir / "static.png"), 0.0, dur))
        sub_center = top + int((bottom - top) * (0.52 if style == "headline" else 0.62))
    else:
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
        if style == "headline":
            img = text_image(sub["text"], 58, max_w=W - 120, max_lines=2, plate=None, stroke=5, min_size=40)
            _frame_png(path, [(img, sub_center, True)])
        elif style == "plain":
            img = text_image(sub["text"], 54, max_w=W - 100, max_lines=2, plate=None, stroke=0, min_size=38,
                             font_path=MINCHO, shadow=True)
            _frame_png(path, [(img, sub_center, True)])
        else:
            _frame_png(path, [(text_image(sub["text"], 64), pos["sub_top"], False)])
        pngs.append((path, s_start, s_end))
    if style == "headline" and notify and app_name:
        path = str(tmp_dir / "notify.png")
        _frame_png(path, [(notification(app_name, notify, lang), 190, True)])
        pngs.append((path, max(0.0, dur - NOTIFY_SEC), dur))

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
        layers, card_bg = end_card_layers(app_name, tagline, style, lang)
        _frame_png(card, layers, bg=card_bg)
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
           "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart",
           "-use_editlist", "0", out_path]
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
