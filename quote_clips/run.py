"""quote_clips/run.py — sources.csv → quote short clips, one version per subtitle language.

Usage:
    .venv/bin/python -m quote_clips.run --sources sources.csv --app-name "アプリ名"

sources.csv columns:
    mode      card (default when empty) / clip
    card rows: quote_id (from the app's data, --quotes-data) or quote[,quote_en] + speaker,
               category, work[,speaker_en,work_en], cite[,cite_en] (text citation, never a URL)
    clip rows: url, speaker, category, work, license (required), author
    category: 偉人 / アニメ, work: 作品名 for anime. *_en are optional English display names
    (looked up / translated with the configured LLM when empty).
    Clips are only for footage whose owner explicitly allows reuse (research/copyright/DECISION.md).
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

from quote_clips.card import load_app_quotes, make_card_video
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


def source_line(lang: str, cite: str = "", url: str = "", author: str = "", license_name: str = "") -> str:
    """Citation line for a post (research/copyright/DECISION.md ⑤).

    Cards: text citation only — never a URL (a fan upload link would point at an infringing copy).
    Licensed clips: original URL + author + licence, which CC BY requires.
    """
    if url:
        parts = [p for p in (url, author, license_name) if p]
        return ("Source: " if lang == "en" else "出典: ") + " / ".join(parts)
    if cite:
        return ("Source: " if lang == "en" else "出典: ") + cite
    return ""


def build_meta(lang: str, names: Dict[str, str], category: str, cand: Dict, source: str, music_note: str,
               source_url: str = "") -> Dict:
    """Post title / description / hashtags for one language version."""
    speaker, work = names["speaker"], names["work"]
    if lang == "en":
        tags = ["#quotes", "#motivation", "#mindset"]
        if category == "アニメ":
            tags.append("#animequotes")
        quote = cand.get("quote_en", "")
        lines = [f"\u201c{quote}\u201d" if quote else "", f"\u2014 {telop_text(speaker, category, work, 'en')}", "",
                 source]
        title = cand.get("title_en") or cand.get("hook_en", "").replace("\n", " ") or speaker or "Quote"
    else:
        tags = ["#名言", "#モチベーション"]
        if category == "アニメ":
            tags.append("#アニメ名言")
        quote = cand.get("quote_ja", "")
        lines = [f"「{quote}」" if quote else "", f"\u2014 {telop_text(speaker, category, work)}", "", source]
        title = cand.get("title_ja") or cand.get("hook_ja", "").replace("\n", " ") or speaker or "名言"
    tags += [_tag(x) for x in (speaker, work) if x and len(_tag(x)) > 1]
    desc = "\n".join(filter(None, lines + [music_note, " ".join(tags)]))
    return {"lang": lang, "title": title, "description": desc, "hashtags": tags, "source_url": source_url,
            "speaker": speaker, "quote": quote, "start": cand.get("start", 0.0), "end": cand.get("end", 0.0)}


def row_mode(row: Dict) -> str:
    """Empty mode means card, so a forgotten column can never publish someone else's footage."""
    return (row.get("mode") or "").strip().lower() or "card"


def _credits(music_dir: str) -> Dict[str, str]:
    manifest = Path(music_dir) / "manifest.json"
    if not manifest.exists():
        return {}
    return {t["file"]: t["attribution"] for t in json.loads(manifest.read_text(encoding="utf-8"))}


def card_fields(row: Dict, app_quotes: Dict[str, Dict]) -> Dict[str, str]:
    """Card text for both languages, from the app's quote data (quote_id) and/or the row itself."""
    get = lambda k: (row.get(k) or "").strip()  # noqa: E731
    q = app_quotes.get(get("quote_id"), {})
    if get("quote_id") and not q:
        raise ValueError(f"quote_id {get('quote_id')} not found in the app quote data")
    category = get("category") or ("アニメ" if q.get("kind") == "anime" else "偉人")
    return {
        "id": get("quote_id") or "",
        "category": category,
        "quote_ja": get("quote") or q.get("text_ja", ""),
        "quote_en": get("quote_en") or q.get("text_en", ""),
        "speaker_ja": get("speaker") or q.get("speaker_ja", ""),
        "speaker_en": get("speaker_en") or q.get("speaker_en", ""),
        "work_ja": get("work") or q.get("work_ja", ""),
        "work_en": get("work_en") or q.get("work_en", ""),
        "cite_ja": get("cite") or q.get("source", ""),
        "cite_en": get("cite_en") or q.get("source_en", ""),
    }


def process_card(row: Dict, args: argparse.Namespace, app_quotes: Dict[str, Dict]) -> List[str]:
    f = card_fields(row, app_quotes)
    if not f["quote_ja"] and not f["quote_en"]:
        raise ValueError("card row needs quote_id or quote")
    if not f["speaker_ja"] and not f["speaker_en"]:
        raise ValueError("card row needs a speaker (never guessed)")
    # Fill a missing language: names via the LLM, wording via the subtitle translator.
    if "en" in args.langs and (not f["speaker_en"] or (f["work_ja"] and not f["work_en"])):
        looked_up = english_names(f["speaker_ja"], f["work_ja"])
        f["speaker_en"] = f["speaker_en"] or looked_up["speaker_en"]
        f["work_en"] = f["work_en"] or looked_up["work_en"]
    for lang, other in (("en", "ja"), ("ja", "en")):
        if lang in args.langs and not f[f"quote_{lang}"] and f[f"quote_{other}"]:
            f[f"quote_{lang}"] = subtitle_lines([f[f"quote_{other}"]], f[f"speaker_{other}"], other, lang)[0].replace("\n", "")
        if lang in args.langs and not f[f"cite_{lang}"]:
            f[f"cite_{lang}"] = f[f"cite_{other}"] if f[f"cite_{other}"].isascii() or lang == "ja" else ""

    key = f["id"] or _slug(f["quote_ja"] or f["quote_en"])
    credits = _credits(args.music_dir)
    outputs = []
    for lang in args.langs:
        quote = f[f"quote_{lang}"]
        if not quote:
            logger.warning(f"[card {key}] no {lang} text; skipped")
            continue
        out_dir = Path(args.out_dir) / lang / "cards"
        out_dir.mkdir(parents=True, exist_ok=True)
        speaker = f[f"speaker_{lang}"] or f["speaker_ja"]
        final = out_dir / f"{key}_{_slug(speaker)}.mp4"
        if final.exists():
            logger.info(f"Exists, skipped: {final}")
            continue
        track = make_card_video(quote, speaker, f[f"work_{lang}"], f[f"cite_{lang}"], lang, str(final),
                                seed=f"{key}:{lang}", app_name=args.app_name_en if lang == "en" else args.app_name,
                                music_dir=args.music_dir, use_bgm=not args.no_bgm, tagline=args.app_tagline)
        music_note = credits.get(Path(track).name, "") if track else ""
        names = {"speaker": speaker, "work": f[f"work_{lang}"]}
        cand = {f"quote_{lang}": quote, f"title_{lang}": quote.replace("\n", " ")[:40]}
        cite = " ".join(x for x in (f"\u2014 {telop_text(speaker, f['category'], f[f'work_{lang}'], lang)}",
                                     f[f"cite_{lang}"]) if x)
        meta = build_meta(lang, names, f["category"], cand, source_line(lang, cite=f[f"cite_{lang}"]), music_note)
        meta.update({"mode": "card", "quote_id": f["id"], "cite": cite})
        final.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Done: {final}")
        outputs.append(str(final))
    return outputs


def process_row(row: Dict, args: argparse.Namespace) -> List[str]:
    url = (row.get("url") or "").strip()
    license_name = (row.get("license") or "").strip()
    if not license_name:
        # A clip is allowed only when a person wrote down the owner's reuse licence (DECISION.md ②).
        raise ValueError(f"clip row without license, skipped: {url}")
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

    credits = _credits(args.music_dir)
    source = {lang: source_line(lang, url=url, author=(row.get("author") or "").strip(), license_name=license_name)
              for lang in args.langs}
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
                meta = build_meta(lang, n, category, cand, source[lang], music_note, source_url=url)
                meta.update({"mode": "clip", "license": license_name, "author": (row.get("author") or "").strip()})
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
    p.add_argument("--quotes-data", default=os.getenv("QUOTES_DATA", "~/meigen-app/data"),
                   help="App quote data folder for card rows that use quote_id")
    args = p.parse_args()
    args.langs = [x.strip() for x in args.langs.split(",") if x.strip()]
    args.app_name_en = args.app_name_en or args.app_name

    with open(args.sources, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())
                and not next(iter(r.values()), "").startswith("#")]
    if not rows:
        logger.error(f"No rows in {args.sources}")
        sys.exit(1)

    app_quotes = load_app_quotes(args.quotes_data)
    total: List[str] = []
    for row in rows:
        try:
            if row_mode(row) == "card":
                if not args.select_only:
                    total += process_card(row, args, app_quotes)
            else:
                total += process_row(row, args)
        except Exception as e:
            logger.error(f"Source failed ({row.get('quote_id') or row.get('url')}): {e}")
    logger.info(f"=== {len(total)} clips generated ===")
    for t in total:
        logger.info(f"  {t}")


if __name__ == "__main__":
    main()
