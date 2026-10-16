"""quote_clips/selector.py — Ask an LLM for quote moments, snapped to segment boundaries.

Remixed from clip_pipeline/highlight_selector.py: the prompt targets 名言・決め台詞
instead of generic viral moments, and works on sentence segments.
"""

from typing import Dict, List

from loguru import logger

from quote_clips.llm import call_llm, parse_json

MIN_CLIP_SEC = 10.0
MAX_CLIP_SEC = 60.0
CHUNK_SEGMENTS = 150
CHUNK_OVERLAP = 15

PROMPT = """あなたはモチベーション系ショート動画の編集者だ。
以下は{who}の文字起こし（[番号] 開始秒-終了秒 テキスト）。
視聴者のやる気を上げる「名言・決め台詞」が含まれる区間を最大{max_quotes}件選べ。

条件:
- 区間は{min_sec:.0f}〜{max_sec:.0f}秒。名言の前に必要な最小限の文脈だけ含める。
- 開始と終了は必ず文字起こしの区切り（番号）に合わせ、文の途中で切らない。
- 単体で意味が通り、心に刺さるものだけ。雑談・内輪ネタ・宣伝は除外。
- 該当がなければ [] だけを返す。

出力はJSON配列のみ（説明文禁止）:
[
  {{
    "start_index": 12,
    "end_index": 18,
    "quote_ja": "名言の核となる一文（日本語。原文が外国語なら自然な日本語訳）",
    "quote_en": "the same core quote in English (original wording if the speech is English)",
    "hook_ja": "画面上部に出す引きの一言（日本語、20文字以内。12文字を超えるなら意味の切れ目に\\nを1つ入れる）",
    "hook_en": "the hook in English (max 40 chars; if over 22 chars put one \\n between natural phrases)",
    "title_ja": "投稿タイトル（日本語、30文字以内）",
    "title_en": "post title in English (max 60 chars)",
    "score": 1から10の整数（刺さる強さ）
  }}
]

TRANSCRIPT:
{transcript}"""


def _format(segments: List[Dict], lo: int, hi: int) -> str:
    return "\n".join(
        f"[{i}] {s['start']:.1f}-{s['end']:.1f} {s['text']}" for i, s in enumerate(segments[lo:hi], start=lo)
    )


def select_quotes(
    segments: List[Dict],
    speaker: str,
    work: str = "",
    max_quotes: int = 3,
) -> List[Dict]:
    """Return candidates sorted by score: {start, end, quote_ja, hook_ja, title_ja, score}."""
    if not segments:
        return []
    work_note = f"（作品『{work}』）" if work else ""
    if speaker:
        who = f"「{speaker}」{work_note}が話している動画"
    else:
        who = f"複数の人物が話す動画{work_note}"
    found: List[Dict] = []

    lo = 0
    while lo < len(segments):
        hi = min(len(segments), lo + CHUNK_SEGMENTS)
        prompt = PROMPT.format(
            who=who,
            max_quotes=max_quotes,
            min_sec=MIN_CLIP_SEC,
            max_sec=MAX_CLIP_SEC,
            transcript=_format(segments, lo, hi),
        )
        try:
            parsed = parse_json(call_llm(prompt))
        except Exception as e:
            logger.warning(f"LLM failed for segments {lo}-{hi}: {e}")
            parsed = None
        for c in parsed if isinstance(parsed, list) else []:
            cand = _snap(c, segments)
            if cand:
                found.append(cand)
        if hi == len(segments):
            break
        lo = hi - CHUNK_OVERLAP

    result = _dedupe(sorted(found, key=lambda c: c["score"], reverse=True))[:max_quotes]
    logger.info(f"Selected {len(result)} quote clips (from {len(found)} raw candidates)")
    return result


def _snap(c: Dict, segments: List[Dict]) -> Dict | None:
    """Convert segment indices to times and validate duration."""
    try:
        si, ei = int(c["start_index"]), int(c["end_index"])
        si, ei = max(0, si), min(len(segments) - 1, ei)
        if ei < si:
            return None
        start, end = segments[si]["start"], segments[ei]["end"]
    except (KeyError, ValueError, TypeError, IndexError):
        return None
    if not (MIN_CLIP_SEC <= end - start <= MAX_CLIP_SEC):
        logger.debug(f"skip duration {end - start:.1f}s")
        return None
    return {
        "start": float(start),
        "end": float(end),
        "quote_ja": str(c.get("quote_ja", "")).strip(),
        "quote_en": str(c.get("quote_en", "")).strip(),
        "hook_ja": _clean(c.get("hook_ja"))[:26],
        "hook_en": _clean(c.get("hook_en"))[:48],
        "title_ja": str(c.get("title_ja", "")).strip()[:40],
        "title_en": str(c.get("title_en", "")).strip()[:80],
        "score": int(c.get("score", 0) or 0),
    }


def _clean(v: object) -> str:
    """Normalize a short text field; keeps intentional line breaks."""
    return str(v or "").replace("\\n", "\n").strip()


def _dedupe(cands: List[Dict], min_gap: float = 5.0) -> List[Dict]:
    """Drop candidates overlapping an already-kept (higher score) one."""
    kept: List[Dict] = []
    for c in cands:
        if all(c["end"] <= k["start"] + min_gap or c["start"] >= k["end"] - min_gap for k in kept):
            kept.append(c)
    return kept
