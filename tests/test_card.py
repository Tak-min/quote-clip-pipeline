"""Card mode and citation rules (research/copyright/DECISION.md); no rendering, no API calls."""

import argparse
import json

import pytest

from quote_clips import card, run


def test_empty_mode_is_card():
    assert run.row_mode({"mode": ""}) == "card"
    assert run.row_mode({}) == "card"
    assert run.row_mode({"mode": "Clip"}) == "clip"


def test_clip_without_license_is_refused():
    with pytest.raises(ValueError, match="license"):
        run.process_row({"mode": "clip", "url": "https://example.com/v"}, argparse.Namespace())


def test_card_citation_is_text_never_url():
    line = run.source_line("ja", cite="スタンフォード大学卒業式スピーチ、2005年")
    assert line == "出典: スタンフォード大学卒業式スピーチ、2005年" and "http" not in line
    assert run.source_line("en", cite="") == ""


def test_licensed_clip_citation_has_url_author_and_license():
    line = run.source_line("en", url="https://x/v", author="NASA", license_name="Public domain")
    assert line == "Source: https://x/v / NASA / Public domain"


def test_build_meta_uses_given_source_line():
    meta = run.build_meta("ja", {"speaker": "武田一鉄", "work": "ハイキュー!!"}, "アニメ",
                          {"quote_ja": "負けは弱さの証明ではない"}, "出典: 42巻 第369話", "Music: x")
    assert "出典: 42巻 第369話" in meta["description"] and "http" not in meta["description"]
    assert meta["source_url"] == "" and "#アニメ名言" in meta["hashtags"]


def test_card_fields_come_from_app_quote_data(tmp_path):
    (tmp_path / "works.json").write_text(json.dumps([{"id": "haikyu", "ja": "ハイキュー!!", "en": "Haikyu!!"}]))
    (tmp_path / "quotes_anime.json").write_text(json.dumps([{
        "id": "ani-004", "kind": "anime", "text_ja": "負けは", "text_en": "A loss", "speaker_ja": "武田一鉄",
        "speaker_en": "Ittetsu Takeda", "work_id": "haikyu", "source": "ハイキュー!! 42巻", "source_en": "Vol. 42"}]),
        encoding="utf-8")
    quotes = card.load_app_quotes(str(tmp_path))
    f = run.card_fields({"quote_id": "ani-004"}, quotes)
    assert f["category"] == "アニメ" and f["work_en"] == "Haikyu!!" and f["cite_en"] == "Vol. 42"
    with pytest.raises(ValueError, match="not found"):
        run.card_fields({"quote_id": "nope"}, quotes)


def test_strip_work_from_citation():
    assert card._strip_work("ハイキュー!! 42巻 第369話", "ハイキュー!!") == "42巻 第369話"
    assert card._strip_work("Vol. 21", "Haikyu!!") == "Vol. 21"


def test_line_break_rejects_reworded_text(monkeypatch):
    monkeypatch.setattr(card, "call_llm", lambda *a, **k: '{"text": "負けは\\n強さだ"}')
    assert card.break_lines("負けは弱さではない", "ja") == "負けは弱さではない"
    monkeypatch.setattr(card, "call_llm", lambda *a, **k: '{"text": "負けは\\n弱さではない"}')
    assert card.break_lines("負けは弱さではない", "ja") == "負けは\n弱さではない"


def test_timeline_keeps_reading_time_in_bounds():
    layout = card.card_layout("短い", "誰か", "", "", "ja", seed="s")
    _, dur = card.card_timeline(layout, "ja")
    assert 6.0 <= dur <= 14.0
