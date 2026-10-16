"""Text LLM routing for quote selection, translation, and subtitle formatting.

Groq is the primary provider.  Keys and models are tried in a deterministic
order, then Gemini is used as the last-resort provider.  Secret values are
never logged.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping, Optional

import requests
from dotenv import dotenv_values
from loguru import logger

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

GROQ_KEY_NAMES = (
    "GROQ_API_KEY_MOTIVATION",
    "GROQ_API_KEY_DAILY_CURIOSITY",
    "GROQ_API_KEY_ANIMAL_MICRODRAMA",
    "GROQ_API_KEY",
)
GEMINI_KEY_NAMES = (
    "GEMINI_API_KEY_MOTIVATION",
    "GEMINI_API_KEY_ANIMAL_MICRODRAMA",
    "GEMINI_API_KEY",
)
DEFAULT_GROQ_MODELS = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b")
DEFAULT_GEMINI_MODELS = ("gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-flash-lite")
DEFAULT_SHARED_ENV = Path("~/youtube-ai-pipeline/docker/.env").expanduser()


def _split(value: str, defaults: tuple[str, ...]) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or list(defaults)


def _config() -> dict[str, str]:
    """Merge the shared key file with the process environment.

    ``LLM_ENV_FILE`` can point elsewhere.  The existing YouTube pipeline file
    is used only when present, so this project remains portable.
    """
    env_path = Path(os.getenv("LLM_ENV_FILE", str(DEFAULT_SHARED_ENV))).expanduser()
    values: dict[str, str] = {}
    if env_path.is_file():
        values.update({k: str(v) for k, v in dotenv_values(env_path).items() if v})
    values.update({k: v for k, v in os.environ.items() if v})
    return values


def _named_keys(config: Mapping[str, str], names: tuple[str, ...]) -> list[tuple[str, str]]:
    """Return configured keys in priority order, removing duplicate values."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in names:
        value = config.get(name, "").strip()
        if value and value not in seen:
            found.append((name, value))
            seen.add(value)
    return found


def _groq_call(prompt: str, model: str, key: str, temperature: float) -> requests.Response:
    return requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_completion_tokens": 8192,
        },
        timeout=120,
    )


def _gemini_call(prompt: str, model: str, key: str, temperature: float) -> requests.Response:
    return requests.post(
        GEMINI_URL.format(model=model),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        json={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 8192, "temperature": temperature},
        },
        timeout=120,
    )


def _response_error(provider: str, model: str, key_name: str, status: object) -> RuntimeError:
    return RuntimeError(f"{provider}/{model} via {key_name} returned {status}")


def _retry_after(response: requests.Response) -> Optional[float]:
    """Seconds the API asked us to wait (Retry-After header), if any."""
    value = (getattr(response, "headers", None) or {}).get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _provider_chain(provider: str, config: Mapping[str, str], order: list[str], model: Optional[str]):
    if provider == "groq":
        keys = _named_keys(config, GROQ_KEY_NAMES)
        configured = config.get("LLM_GROQ_MODELS", "")
        models = _split(configured, DEFAULT_GROQ_MODELS)
        if model and not configured:
            models = [model] + [m for m in models if m != model]
        return keys, models, _groq_call
    if provider == "gemini":
        keys = _named_keys(config, GEMINI_KEY_NAMES)
        configured = config.get("LLM_GEMINI_MODELS", "")
        if configured:
            models = _split(configured, DEFAULT_GEMINI_MODELS)
        else:
            # Keep the legacy single-model setting first, but never let an
            # obsolete model suppress the maintained fallback chain.
            legacy = config.get("GEMINI_MODEL", "").strip()
            models = ([legacy] if legacy else []) + [m for m in DEFAULT_GEMINI_MODELS if m != legacy]
        if model and not configured and not config.get("GEMINI_MODEL") and order[0] == "gemini":
            models = [model] + [m for m in models if m != model]
        return keys, models, _gemini_call
    return None


def _try_provider(provider, keys, models, caller, prompt, temperature, errors):
    """One pass over every model x key.

    Returns ``(text, waits)``.  ``waits`` holds one entry per temporary failure
    (429 / 5xx / network): the server's Retry-After or ``None``.  An empty
    ``waits`` means every failure was permanent, so waiting would not help.
    """
    waits: list[Optional[float]] = []
    for candidate_model in models:
        model_missing = False
        for key_name, key in keys:
            try:
                response = caller(prompt, candidate_model, key, temperature)
            except requests.RequestException as exc:
                errors.append(exc)
                waits.append(None)
                logger.warning(f"{provider}/{candidate_model} network error via {key_name}; rotating key")
                continue

            if response.status_code == 200:
                data = response.json()
                if provider == "groq":
                    choices = data.get("choices") or []
                    text = choices[0].get("message", {}).get("content", "") if choices else ""
                else:
                    candidates = data.get("candidates") or []
                    parts = candidates[0].get("content", {}).get("parts") or [] if candidates else []
                    text = "".join(part.get("text", "") for part in parts)
                if text:
                    logger.info(f"LLM used {provider}/{candidate_model} via {key_name}")
                    return text, waits
                errors.append(RuntimeError(f"Empty response from {provider}/{candidate_model}"))
                continue

            status = response.status_code
            errors.append(_response_error(provider, candidate_model, key_name, status))
            if status == 404:
                logger.warning(f"{provider}/{candidate_model} not found; switching model")
                model_missing = True
                break
            if status == 429 or status >= 500:
                waits.append(_retry_after(response))
                logger.warning(f"{provider}/{candidate_model} returned {status} via {key_name}; rotating key")
                continue
            if status in (401, 403):
                logger.warning(f"{provider}/{candidate_model} returned {status} via {key_name}; rotating key")
                continue
            logger.warning(f"{provider}/{candidate_model} rejected the request ({status})")
            break
        if model_missing:
            continue
    return None, waits


def _wait_seconds(waits: list[Optional[float]], attempt: int, base: float, max_wait: float) -> Optional[float]:
    """How long to sleep before retrying a provider, or None when waiting is pointless.

    Any one key recovering is enough, so the shortest Retry-After wins.  Without
    hints, back off exponentially.  A hint longer than ``max_wait`` (e.g. a daily
    quota) means the next provider should be used instead of waiting.
    """
    if not waits:
        return None
    hints = [w for w in waits if w is not None]
    seconds = min(hints) if hints else base * (2 ** attempt)
    return None if seconds > max_wait else max(seconds, 1.0)


def call_llm(prompt: str, model: Optional[str] = None, temperature: float = 0.4) -> str:
    """Call Groq first, rotating keys/models, then fall back to Gemini.

    When every key of a provider is temporarily rate-limited, wait (Retry-After
    or exponential backoff) and retry that provider before moving on, so short
    per-minute limits do not burn the Gemini fallback.  ``LLM_RETRY_ROUNDS``,
    ``LLM_BACKOFF_BASE`` and ``LLM_MAX_WAIT`` (seconds) tune this.

    ``model`` applies to the first provider for backward compatibility.  Use
    ``LLM_GROQ_MODELS`` and ``LLM_GEMINI_MODELS`` for explicit model chains.
    """
    config = _config()
    order = _split(config.get("LLM_PROVIDER_ORDER", "groq,gemini"), ("groq", "gemini"))
    retry_rounds = int(config.get("LLM_RETRY_ROUNDS", "3"))
    backoff_base = float(config.get("LLM_BACKOFF_BASE", "5"))
    max_wait = float(config.get("LLM_MAX_WAIT", "60"))
    errors: list[Exception] = []

    for provider in order:
        chain = _provider_chain(provider, config, order, model)
        if chain is None:
            logger.warning(f"Unknown LLM provider skipped: {provider}")
            continue
        keys, models, caller = chain
        if not keys:
            errors.append(RuntimeError(f"No {provider} API keys configured"))
            continue

        for attempt in range(retry_rounds + 1):
            text, waits = _try_provider(provider, keys, models, caller, prompt, temperature, errors)
            if text:
                return text
            seconds = _wait_seconds(waits, attempt, backoff_base, max_wait) if attempt < retry_rounds else None
            if seconds is None:
                break
            logger.warning(f"All {provider} keys busy; waiting {seconds:.0f}s before retry {attempt + 1}/{retry_rounds}")
            time.sleep(seconds)

    detail = "; ".join(str(error) for error in errors[-4:]) or "no configured providers"
    raise RuntimeError(f"All LLM providers failed: {detail}")


def parse_json(raw: str) -> Optional[Any]:
    """Extract a JSON array/object from LLM output, repairing common issues."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if not match:
        return None
    candidate = match.group()
    for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
        attempt = re.sub(r"[\x00-\x1f]", " ", attempt)
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue
    logger.warning("JSON repair failed")
    return None
