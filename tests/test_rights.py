import argparse
import json

import pytest

from quote_clips import rights, run

FULL = {"rights_holder": "Committee", "granted_by": "CEO", "granted_on": "2026-11-10", "credit": "(c) Committee",
        "commercial": True, "platforms": ["youtube", "tiktok", "instagram"], "work_ja": "作品", "work_en": "Work",
        "sources": [{"url": "https://example.com/v"}]}
ALL = ["youtube", "tiktok", "instagram"]


def _write(tmp_path, works):
    p = tmp_path / "permissions.json"
    p.write_text(json.dumps({"works": works}))
    return p


def test_complete_entry_is_permitted(tmp_path):
    p = _write(tmp_path, {"w": FULL})
    assert rights.find("w", ALL, p)["work_id"] == "w"
    assert rights.find("作品", ALL, p) and rights.find("Work", ALL, p)


@pytest.mark.parametrize("change", [
    {"rights_holder": ""}, {"commercial": False}, {"platforms": ["youtube"]}, {"sources": []}, {"granted_on": ""},
])
def test_incomplete_entry_is_not_permitted(tmp_path, change):
    p = _write(tmp_path, {"w": {**FULL, **change}})
    assert rights.find("w", ALL, p) is None


def test_shipped_registry_entries_are_complete():
    works = rights.load()
    assert works and all(not rights.problems(v, ALL) for v in works.values())
    assert "ippo" not in works and "worldtrigger" not in works  # not in the 2026-11-10 別紙


def test_anime_clip_row_without_permission_is_refused(monkeypatch):
    monkeypatch.setattr(run, "download_and_transcribe", lambda **k: pytest.fail("must not download"))
    row = {"mode": "clip", "url": "https://example.com/v", "category": "アニメ", "work": "はじめの一歩",
           "license": "whatever"}
    with pytest.raises(ValueError, match="permission"):
        run.process_row(row, argparse.Namespace())
