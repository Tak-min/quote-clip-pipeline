"""quote_clips/run.py — sources.csv → quote short clips, one version per subtitle language.

Usage:
    .venv/bin/python -m quote_clips.run --sources sources.csv --app-name "アプリ名"

sources.csv columns: url,speaker,category,work[,speaker_en,work_en]
    category: 偉人 / アニメ, work: 作品名 for anime. *_en are optional English display names
    (looked up with the configured LLM when empty).
Output (one folder per account language):
    output/ja/<video_id>/NN_<title>.mp4 + .json
    output/en/<video_id>/NN_<title>.mp4 + .json
"""

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv
from loguru import logger

from quote_clips.ingest import download_and_transcribe
from quote_clips.render import compose, prepare_base
from quote_clips.selector import select_quotes
from quote_clips.translate import english_names, subtitle_cards, subtitle_lines


def clip_subtitles(segments: List[Dict], start: float, end: float) -> List[Dict]:
    """Segments overlapping [start, end], re-zeroed to the clip start."""
    out = []
    for s in segments:
        if s["end"] <= start or s["start"] >= end:
            continue
        out.append({
            "text": s["text"],
            "start": round(max(0.0, s["start"] - start), 3),
            "end": round(min(end, s["end"]) - start, 3),
        })
    return out


_HARD_END = tuple(".?!。？！")
_SOFT_END = tuple(",、，;:")
# (soft-break minimum, hard maximum) characters per subtitle card, by audio language
_CARD_LEN = {"ja": (10, 30), "default": (28, 80)}


def clip_words(words: List[Dict], start: float, end: float) -> List[Dict]:
    """Words inside [start, end], re-zeroed to the clip start."""
    return [{"text": w["text"], "start": round(max(0.0, w["start"] - start), 3), "end": round(min(end, w["end"]) - start, 3)}
            for w in words if w["start"] >= start - 0.05 and w["end"] <= end + 0.05 and w["text"].strip()]


def chunk_words(words: List[Dict], start: float, end: float, lang: str) -> List[Dict]:
    """Group word timestamps into subtitle cards that end at sentence/clause boundaries.

    Whisper's segments often end mid-sentence ("...fill a large part of"); cards built
    from words break at . ? ! first, then at commas / pauses once long enough.
    """
    soft_min, hard_max = _CARD_LEN.get(lang, _CARD_LEN["default"])
    sep = "" if lang in ("ja", "zh") else " "
    ws = [w for w in words if w["start"] >= start - 0.05 and w["end"] <= end + 0.05 and w["text"].strip()]
    cards: List[Dict] = []
    cur: List[Dict] = []

    def flush() -> None:
        if cur:
            text = sep.join(w["text"].strip() for w in cur).strip()
            cards.append({"text": text, "start": round(max(0.0, cur[0]["start"] - start), 3),
                          "end": round(min(end, cur[-1]["end"]) - start, 3)})
            cur.clear()

    for i, w in enumerate(ws):
        cur.append(w)
        text_len = len(sep.join(x["text"].strip() for x in cur))
        token = w["text"].strip()
        gap = ws[i + 1]["start"] - w["end"] if i + 1 < len(ws) else 0.0
        if (token.endswith(_HARD_END)
                or (token.endswith(_SOFT_END) and text_len >= soft_min)
                or (gap >= 0.6 and text_len >= soft_min // 2)
                or text_len >= hard_max):
            flush()
    flush()
    return cards


def split_cards(subs: List[Dict], max_lines: int = 2) -> List[Dict]:
    """Split subtitles with more than max_lines visual lines into consecutive cards.

    Time is shared in proportion to character count, so each card stays on screen
    roughly while its words are spoken. Line breaks themselves come from Gemini.
    """
    out: List[Dict] = []
    for s in subs:
        lines = [ln for ln in s["text"].split("\n") if ln.strip()]
        if len(lines) <= max_lines:
            out.append(s)
            continue
        chunks = ["\n".join(lines[i:i + max_lines]) for i in range(0, len(lines), max_lines)]
        total = sum(len(c) for c in chunks)
        t = s["start"]
        span = s["end"] - s["start"]
        for c in chunks:
            d = span * len(c) / total
            out.append({"text": c, "start": round(t, 3), "end": round(t + d, 3)})
            t += d
    return out


def telop_text(speaker: str, category: str, work: str, lang: str = "ja") -> str:
    """Speaker name as written in sources.csv. Blank speaker → work title only (never guessed)."""
    if lang == "en":
        if speaker and work:
            return f"{speaker} ({work})"
        return speaker or work
    if speaker and work:
        return f"{speaker}（{work}）"
    return speaker or (f"『{work}』" if work else "")


def _slug(text: str) -> str:
    return re.sub(r"[^\w\-ぁ-んァ-ン一-龥ー]", "_", text)[:30].strip("_") or "clip"


def _tag(text: str) -> str:
    return "#" + re.sub(r"[^\wぁ-んァ-ン一-龥ー]", "", text)


def build_meta(lang: str, names: Dict[str, str], category: str, cand: Dict, url: str, music_note: str) -> Dict:
    """Post title / description / hashtags for one language version."""
    speaker, work = names["speaker"], names["work"]
    if lang == "en":
        tags = ["#quotes", "#motivation", "#mindset"]
        if category == "アニメ":
            tags.append("#animequotes")
        quote = cand.get("quote_en", "")
        lines = [f"\u201c{quote}\u201d" if quote else "", f"\u2014 {telop_text(speaker, category, work, 'en')}", "",
                 f"Source: {url}"]
        title = cand.get("title_en") or cand.get("hook_en", "").replace("\n", " ") or speaker or "Quote"
    else:
        tags = ["#名言", "#モチベーション"]
        if category == "アニメ":
            tags.append("#アニメ名言")
        quote = cand.get("quote_ja", "")
        lines = [f"「{quote}」" if quote else "", f"\u2014 {telop_text(speaker, category, work)}", "", f"出典: {url}"]
        title = cand.get("title_ja") or cand.get("hook_ja", "").replace("\n", " ") or speaker or "名言"
    tags += [_tag(x) for x in (speaker, work) if x and len(_tag(x)) > 1]
    desc = "\n".join(filter(None, lines + [music_note, " ".join(tags)]))
    return {"lang": lang, "title": title, "description": desc, "hashtags": tags, "source_url": url,
            "speaker": speaker, "quote": quote, "start": cand["start"], "end": cand["end"]}


def process_row(row: Dict, args: argparse.Namespace) -> List[str]:
    url = row["url"].strip()
    speaker = (row.get("speaker") or "").strip()
    work = (row.get("work") or "").strip()
    category = (row.get("category") or "").strip()

    transcript = download_and_transcribe(
        url=url, work_dir=args.work_dir, model_size=args.model_size,
        language=(row.get("language") or None), device=args.device,
    )
    segments = transcript.get("segments") or []
    src_lang = transcript.get("language", "ja")
    video_id = transcript["video_id"]
    logger.info(f"{video_id}: lang={src_lang}, {len(segments)} segments, speaker={speaker}")

    cands = select_quotes(segments, speaker=speaker, work=work, max_quotes=args.max_clips)
    cand_dir = Path(args.out_dir) / "_candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    (cand_dir / f"{video_id}.json").write_text(json.dumps(cands, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.select_only or not cands:
        return []

    names = {"ja": {"speaker": speaker, "work": work}}
    if "en" in args.langs:
        en = {"speaker_en": (row.get("speaker_en") or "").strip(), "work_en": (row.get("work_en") or "").strip()}
        if (speaker and not en["speaker_en"]) or (work and not en["work_en"]):
            looked_up = english_names(speaker, work)
            en = {k: en[k] or looked_up[k] for k in en}
        names["en"] = {"speaker": en["speaker_en"], "work": en["work_en"]}

    manifest = Path(args.music_dir) / "manifest.json"
    credits = {t["file"]: t["attribution"] for t in json.loads(manifest.read_text(encoding="utf-8"))} if manifest.exists() else {}
    outputs = []
    for i, cand in enumerate(cands, start=1):
        words = transcript.get("words") or []
        raw = (chunk_words(words, cand["start"], cand["end"], src_lang) if words
               else clip_subtitles(segments, cand["start"], cand["end"]))
        base = str(cand_dir / f"_{video_id}_{i:02d}_base.mp4")
        try:
            picture = prepare_base(transcript["video_path"], cand["start"], cand["end"], base, layout=args.layout)
            cw = clip_words(words, cand["start"], cand["end"])
            for lang in args.langs:
                cards = subtitle_cards(cw, speaker, src_lang, lang) if cw else None
                if cards is None:  # rule-based fallback: sentence/pause cards + per-card translation
                    texts = subtitle_lines([s["text"] for s in raw], speaker, src_lang, lang)
                    cards = [{**s, "text": t} for s, t in zip(raw, texts)]
                subs = split_cards(cards)
                title = cand.get(f"title_{lang}") or speaker
                out_dir = Path(args.out_dir) / lang / video_id
                out_dir.mkdir(parents=True, exist_ok=True)
                final = str(out_dir / f"{i:02d}_{_slug(title)}.mp4")
                n = names[lang]
                try:
                    track = compose(base, subs, hook=cand.get(f"hook_{lang}", ""),
                                    telop=telop_text(n["speaker"], category, n["work"], lang),
                                    app_name=args.app_name_en if lang == "en" else args.app_name,
                                    out_path=final, picture=picture, music_dir=args.music_dir,
                                    use_bgm=not args.no_bgm, tagline=args.app_tagline)
                except Exception as e:
                    logger.error(f"[{video_id} #{i} {lang}] render failed: {e}")
                    continue
                music_note = credits.get(Path(track).name, "") if track else ""  # CC BY: credit the track used
                meta = build_meta(lang, n, category, cand, url, music_note)
                meta["subtitles"] = subs
                Path(final).with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info(f"Done: {final}")
                outputs.append(final)
        except Exception as e:
            logger.error(f"[{video_id} #{i}] failed: {e}")
        finally:
            if os.path.exists(base):
                os.remove(base)
    return outputs


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="Quote clip pipeline")
    p.add_argument("--sources", default="sources.csv")
    p.add_argument("--app-name", default=os.getenv("APP_NAME", ""), help="End-card app name (empty = no end card)")
    p.add_argument("--app-name-en", default=os.getenv("APP_NAME_EN", ""), help="End-card name for the English version (default: --app-name)")
    p.add_argument("--app-tagline", default=os.getenv("APP_TAGLINE", ""))
    p.add_argument("--langs", default="ja,en", help="Subtitle languages to render (comma separated: ja,en)")
    p.add_argument("--work-dir", default="work")
    p.add_argument("--out-dir", default="output")
    p.add_argument("--music-dir", default="assets/music")
    p.add_argument("--model-size", default="turbo", choices=["tiny", "base", "small", "medium", "turbo", "large-v3"])
    p.add_argument("--device", default="cpu")
    p.add_argument("--max-clips", type=int, default=3, help="Max clips per source video")
    p.add_argument("--layout", default="fit", choices=["fit", "crop"])
    p.add_argument("--no-bgm", action="store_true")
    p.add_argument("--select-only", action="store_true", help="Stop after quote selection (no rendering)")
    args = p.parse_args()
    args.langs = [x.strip() for x in args.langs.split(",") if x.strip()]
    args.app_name_en = args.app_name_en or args.app_name

    with open(args.sources, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("url") or "").strip() and not r["url"].startswith("#")]
    if not rows:
        logger.error(f"No rows in {args.sources}")
        sys.exit(1)

    total: List[str] = []
    for row in rows:
        try:
            total += process_row(row, args)
        except Exception as e:
            logger.error(f"Source failed ({row.get('url')}): {e}")
    logger.info(f"=== {len(total)} clips generated ===")
    for t in total:
        logger.info(f"  {t}")


if __name__ == "__main__":
    main()
