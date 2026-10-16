"""quote_clips/translate.py — Turn raw transcript lines into display-ready subtitles with an LLM.

One call per (clip, target language):
- same language as the audio → only obvious speech-recognition errors are fixed
- different language → translated
In both cases the LLM inserts "\\n" at natural phrase boundaries (文節 / clause breaks),
so the renderer never has to cut a line in the middle of a word or phrase.
"""

import json
from typing import Dict, List, Optional

from loguru import logger

from quote_clips.llm import call_llm, parse_json

LANG_RULES = {
    "ja": {
        "name": "日本語",
        "max_chars": 14,
        "break_rule": "改行は文節・意味の切れ目（助詞や読点の直後など）だけで入れ、単語や複合語の途中では絶対に切らない",
    },
    "en": {
        "name": "English",
        "max_chars": 30,
        "break_rule": "break only between natural phrases (before a preposition/conjunction or after a comma); never split a word or a name",
    },
}

PROMPT = """You are formatting subtitles for a short motivational video.
Speaker: {speaker}
Source language of the lines: {src}
Target language: {target_name}

Task:
- {task}
- Keep a 1:1 mapping: exactly {n} output items for {n} input lines, same order.
- Insert "\\n" inside each item so that every visual line is at most {max_chars} characters, {break_rule}.
- Prefer 1-2 visual lines per item; longer items may have more lines.
- Lines that look like background-music lyrics (not spoken dialogue) become "".
- Output a JSON array of strings only.

Input lines:
{lines}"""

TASK_SAME = "Fix only obvious speech-recognition mistakes using context. Do not add content or polish the wording."
TASK_TRANSLATE = ("Translate faithfully into natural {target_name}: keep every idea the speaker says, "
                  "do not drop, add, or summarize meaning; avoid stiff word-for-word phrasing.")


def subtitle_lines(lines: List[str], speaker: str, source_lang: str, target_lang: str) -> List[str]:
    """Return target-language lines (with \\n phrase breaks), same length as input.

    Falls back to the raw lines when the target equals the source language, otherwise to
    empty lines (never show untranslated text in the wrong-language version).
    """
    if not lines:
        return []
    rules = LANG_RULES[target_lang]
    same = source_lang == target_lang
    task = TASK_SAME if same else TASK_TRANSLATE.format(target_name=rules["name"])
    prompt = PROMPT.format(
        speaker=speaker or "unknown",
        src=source_lang,
        target_name=rules["name"],
        task=task,
        n=len(lines),
        max_chars=rules["max_chars"],
        break_rule=rules["break_rule"],
        lines=json.dumps(lines, ensure_ascii=False, indent=0),
    )
    try:
        parsed = parse_json(call_llm(prompt, temperature=0.2))
    except Exception as e:
        logger.warning(f"Subtitle pass ({target_lang}) failed: {e}")
        parsed = None
    if isinstance(parsed, list) and len(parsed) == len(lines):
        return [str(x).replace("\\n", "\n").strip() for x in parsed]
    logger.warning(f"Subtitle line count mismatch ({target_lang})")
    return lines if same else ["" for _ in lines]


NAMES_PROMPT = """Give the standard English display names for an English-language video.
Person/character: {speaker}
Work (anime/manga title, may be empty): {work}
Use the official English name/title if one exists; otherwise romanize (Hepburn).
Keep an empty input empty. Output JSON only: {{"speaker_en": "...", "work_en": "..."}}"""


def english_names(speaker: str, work: str) -> Dict[str, str]:
    """English display names for an already-known speaker/work (translation, not identification)."""
    if not speaker and not work:
        return {"speaker_en": "", "work_en": ""}
    try:
        parsed = parse_json(call_llm(NAMES_PROMPT.format(speaker=speaker, work=work), temperature=0.0))
    except Exception as e:
        logger.warning(f"English name lookup failed: {e}")
        parsed = None
    if isinstance(parsed, dict):
        return {
            "speaker_en": str(parsed.get("speaker_en") or "").strip() if speaker else "",
            "work_en": str(parsed.get("work_en") or "").strip() if work else "",
        }
    return {"speaker_en": speaker, "work_en": work}


CARDS_PROMPT = """You are making subtitles for a short motivational video.
Speaker: {speaker}. Audio language: {src}. Subtitle language: {target_name}.

Below are the spoken words with index numbers. Group consecutive words into subtitle cards and write each card's
text in {target_name}.
Rules:
- A card is one complete phrase or clause. Never end a card in the middle of a phrase, word, or name.
- Avoid tiny cards: merge short interjections ("whatever", "you know", 相づち) into a neighbouring card.
- Each card's text has at most 2 visual lines of at most {max_chars} characters; put "\\n" between lines, {break_rule}.
- {task}
- Skip words that are background-music lyrics (leave their indices out).
- Cards must be in order and must not overlap.
Output a JSON array only: [{{"from": first_word_index, "to": last_word_index, "text": "..."}}]

Words:
{words}"""


def subtitle_cards(words: List[Dict], speaker: str, source_lang: str, target_lang: str) -> Optional[List[Dict]]:
    """Segment + translate/proofread + line-break in one LLM call.

    words: [{text, start, end}] relative to the clip. Returns [{text, start, end}] or None when
    the model's answer is unusable (caller falls back to rule-based cards).
    """
    if not words:
        return []
    rules = LANG_RULES[target_lang]
    same = source_lang == target_lang
    task = TASK_SAME if same else TASK_TRANSLATE.format(target_name=rules["name"])
    listing = " ".join(f"{i}:{w['text'].strip()}" for i, w in enumerate(words))
    prompt = CARDS_PROMPT.format(speaker=speaker or "unknown", src=source_lang, target_name=rules["name"],
                                 max_chars=rules["max_chars"], break_rule=rules["break_rule"], task=task, words=listing)
    try:
        parsed = parse_json(call_llm(prompt, temperature=0.2))
    except Exception as e:
        logger.warning(f"Card pass ({target_lang}) failed: {e}")
        return None
    return cards_from_indices(parsed, words)


def cards_from_indices(parsed: object, words: List[Dict]) -> Optional[List[Dict]]:
    """Validate the model's index ranges and turn them into timed cards."""
    if not isinstance(parsed, list) or not parsed:
        return None
    cards: List[Dict] = []
    last_to = -1
    for c in parsed:
        try:
            a, b, text = int(c["from"]), int(c["to"]), str(c["text"]).replace("\\n", "\n").strip()
        except (KeyError, TypeError, ValueError):
            return None
        if not (last_to < a <= b < len(words)):
            logger.warning(f"Card indices invalid ({a}-{b} after {last_to}); falling back")
            return None
        last_to = b
        if text:
            cards.append({"text": text, "start": words[a]["start"], "end": words[b]["end"]})
    return cards
