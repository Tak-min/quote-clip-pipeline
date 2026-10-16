"""Provider routing tests; no real API calls."""

from dataclasses import dataclass, field

from quote_clips import llm


@dataclass
class FakeResponse:
    status_code: int
    payload: dict
    headers: dict = field(default_factory=dict)

    def json(self):
        return self.payload


def test_named_keys_preserves_priority_and_deduplicates():
    config = {"A": "same", "B": "same", "C": "third"}
    assert llm._named_keys(config, ("A", "B", "C")) == [("A", "same"), ("C", "third")]


def test_groq_is_primary(monkeypatch):
    monkeypatch.setattr(llm, "_config", lambda: {
        "LLM_PROVIDER_ORDER": "groq,gemini",
        "GROQ_API_KEY_MOTIVATION": "groq-secret",
        "GEMINI_API_KEY": "gemini-secret",
    })
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse(200, {"choices": [{"message": {"content": "[1]"}}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    assert llm.call_llm("hello") == "[1]"
    assert calls == [llm.GROQ_URL]


def test_rotates_groq_keys_then_falls_back_to_gemini(monkeypatch):
    monkeypatch.setattr(llm, "_config", lambda: {
        "LLM_PROVIDER_ORDER": "groq,gemini",
        "LLM_GROQ_MODELS": "openai/gpt-oss-120b",
        "LLM_GEMINI_MODELS": "gemini-flash-latest",
        "GROQ_API_KEY_MOTIVATION": "groq-1",
        "GROQ_API_KEY_DAILY_CURIOSITY": "groq-2",
        "GEMINI_API_KEY_MOTIVATION": "gemini-1",
    })
    auth_headers = []

    def fake_post(url, **kwargs):
        auth_headers.append(kwargs["headers"])
        if url == llm.GROQ_URL:
            return FakeResponse(429, {})
        return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    monkeypatch.setattr(llm.requests, "post", fake_post)
    assert llm.call_llm("hello") == "ok"
    assert [h.get("Authorization") for h in auth_headers[:2]] == ["Bearer groq-1", "Bearer groq-2"]
    # Groq is retried with exponential backoff (5, 10, 20s) before Gemini is touched.
    assert sleeps == [5.0, 10.0, 20.0]
    assert auth_headers[-1]["x-goog-api-key"] == "gemini-1"
    assert sum("x-goog-api-key" in h for h in auth_headers) == 1


def test_legacy_gemini_model_does_not_suppress_fallbacks(monkeypatch):
    monkeypatch.setattr(llm, "_config", lambda: {
        "LLM_PROVIDER_ORDER": "gemini",
        "GEMINI_MODEL": "retired-model",
        "GEMINI_API_KEY": "gemini-secret",
    })
    urls = []

    def fake_post(url, **kwargs):
        urls.append(url)
        if "retired-model" in url:
            return FakeResponse(404, {})
        return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    assert llm.call_llm("hello") == "ok"
    assert "retired-model" in urls[0]
    assert "gemini-flash-latest" in urls[1]


def _groq_config(**extra):
    return lambda: {
        "LLM_PROVIDER_ORDER": "groq,gemini",
        "LLM_GROQ_MODELS": "openai/gpt-oss-120b",
        "LLM_GEMINI_MODELS": "gemini-flash-latest",
        "GROQ_API_KEY_MOTIVATION": "groq-1",
        "GEMINI_API_KEY_MOTIVATION": "gemini-1",
        **extra,
    }


def test_waits_retry_after_then_succeeds_on_groq(monkeypatch):
    monkeypatch.setattr(llm, "_config", _groq_config())
    responses = [
        FakeResponse(429, {}, {"retry-after": "7"}),
        FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
    ]
    urls = []

    def fake_post(url, **kwargs):
        urls.append(url)
        return responses.pop(0)

    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    monkeypatch.setattr(llm.requests, "post", fake_post)
    assert llm.call_llm("hello") == "ok"
    assert sleeps == [7.0]
    assert urls == [llm.GROQ_URL, llm.GROQ_URL]


def test_long_retry_after_skips_wait_and_uses_gemini(monkeypatch):
    # e.g. a daily quota: waiting would stall the batch, so fall back at once.
    monkeypatch.setattr(llm, "_config", _groq_config(LLM_MAX_WAIT="60"))

    def fake_post(url, **kwargs):
        if url == llm.GROQ_URL:
            return FakeResponse(429, {}, {"retry-after": "3600"})
        return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    monkeypatch.setattr(llm.requests, "post", fake_post)
    assert llm.call_llm("hello") == "ok"
    assert sleeps == []


def test_permanent_errors_do_not_wait(monkeypatch):
    monkeypatch.setattr(llm, "_config", _groq_config(LLM_PROVIDER_ORDER="groq"))
    monkeypatch.setattr(llm.requests, "post", lambda url, **kw: FakeResponse(401, {}))
    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    try:
        llm.call_llm("hello")
    except RuntimeError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("expected failure")
    assert sleeps == []
