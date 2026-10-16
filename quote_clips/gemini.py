"""Backward-compatible imports for code that still references ``gemini``.

The actual router now uses Groq first and Gemini only as fallback.
"""

from quote_clips.llm import call_llm, parse_json


def call_gemini(prompt: str, model: str | None = None, temperature: float = 0.4) -> str:
    """Compatibility alias; routes through the configured provider chain."""
    return call_llm(prompt, model=model, temperature=temperature)


__all__ = ["call_gemini", "parse_json"]
