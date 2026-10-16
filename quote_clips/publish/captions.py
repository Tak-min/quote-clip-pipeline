"""Per-platform caption text built from a clip's metadata (.json written by quote_clips.run)."""

from typing import Dict


def build_captions(meta: Dict) -> Dict:
    title, desc, tags = meta["title"], meta["description"], meta["hashtags"]
    tag_line = " ".join(tags)
    short_tags = " ".join(tags[:5])
    quote = meta.get("quote", "")
    lead = (f"「{quote}」" if meta["lang"] == "ja" else f"“{quote}”") if quote else title
    return {
        "lang": meta["lang"],
        "hashtags": tags,
        # YouTube: title ≤100 chars incl. #Shorts; full description with source/credits.
        "title": _fit(title, 100 - len(" #Shorts")) + " #Shorts",
        "description": desc,
        # TikTok / Instagram / Facebook show one caption block.
        "tiktok": _fit(f"{title}\n{short_tags}", 2200),
        "instagram": _fit(desc, 2200),
        "facebook": desc,
        # Threads: 500 chars, keep it to the quote + a few tags.
        "threads": _fit(f"{lead}\n{short_tags}", 500),
    }


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
