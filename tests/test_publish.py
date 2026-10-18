"""Publish pipeline tests — all HTTP is faked; nothing is posted."""

import json
import os

import pytest

from quote_clips.publish import __main__ as cli
from quote_clips.publish import meta as meta_api
from quote_clips.publish import tiktok
from quote_clips.publish.captions import build_captions
from quote_clips.publish.http import PublishError

META = {"lang": "ja", "title": "題名", "description": "「信じろ」\n— ジョブズ\n出典: u\n#名言 #モチベーション",
        "hashtags": ["#名言", "#モチベーション", "#ジョブズ"], "quote": "信じろ", "mode": "card"}


def test_chunk_plan_small_file_single_chunk():
    assert tiktok.chunk_plan(1_000_000) == (1_000_000, 1, [(0, 999_999)])


def test_chunk_plan_merges_remainder_into_last_chunk():
    size = 25 * 1024 * 1024 + 123
    chunk, count, ranges = tiktok.chunk_plan(size)
    assert count == size // chunk and ranges[-1][1] == size - 1
    assert all(b - a + 1 == chunk for a, b in ranges[:-1])


def test_captions_respect_platform_limits():
    long = {**META, "title": "あ" * 200, "description": "い" * 5000}
    c = build_captions(long)
    assert len(c["title"]) <= 100 and c["title"].endswith("#Shorts")
    assert len(c["instagram"]) <= 2200 and len(c["threads"]) <= 500
    assert build_captions(META)["threads"].startswith("「信じろ」")


def _make_clip(tmp_path, lang="ja", name="01_a"):
    d = tmp_path / "output" / lang / "vid"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.mp4").write_bytes(b"x" * 10)
    (d / f"{name}.json").write_text(json.dumps({**META, "lang": lang}), encoding="utf-8")
    return d / f"{name}.mp4"


def test_post_one_records_results_and_never_double_posts(tmp_path, monkeypatch):
    video = _make_clip(tmp_path)
    calls = []
    monkeypatch.setattr(cli.youtube, "post", lambda v, c, cfg: calls.append("yt") or {"id": "y1"})
    monkeypatch.setattr(cli.meta_api, "post_instagram",
                        lambda v, c, cfg: calls.append("ig") or {"id": "i1", "media_url": "https://cdn/x.mp4"})
    seen_url = {}
    monkeypatch.setattr(cli.meta_api, "post_threads",
                        lambda v, c, cfg, url: seen_url.setdefault("u", url) and {"id": "t1"})
    ledger = {}
    cfg = {"youtube": {}, "instagram": {}, "threads": {}}
    cli.post_one(video, cfg, ["youtube", "instagram", "threads"], ledger, "", False)
    cli.post_one(video, cfg, ["youtube", "instagram", "threads"], ledger, "", False)
    assert calls == ["yt", "ig"]                       # second run posts nothing again
    assert seen_url["u"] == "https://cdn/x.mp4"        # Threads reuses the Instagram CDN URL
    assert set(ledger[str(video)]) == {"youtube", "instagram", "threads"}


def test_failure_on_one_platform_does_not_block_others(tmp_path, monkeypatch):
    video = _make_clip(tmp_path)
    def boom(*a):
        raise PublishError("nope")
    monkeypatch.setattr(cli.tiktok, "post", boom)
    monkeypatch.setattr(cli.youtube, "post", lambda v, c, cfg: {"id": "y1"})
    ledger = {}
    cli.post_one(video, {"tiktok": {}, "youtube": {}}, ["youtube", "tiktok"], ledger, "", False)
    assert "youtube" in ledger[str(video)] and "tiktok" not in ledger[str(video)]
    assert cli.pending_videos(tmp_path / "output", "ja", ledger, ["youtube", "tiktok"]) == [video]


class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._d


def test_instagram_flow_uploads_local_file_then_publishes(tmp_path, monkeypatch):
    video = _make_clip(tmp_path)
    monkeypatch.setenv("TOK", "secret")
    log = []

    def fake_post(url, **kw):
        log.append(("POST", url, kw.get("headers", {}).get("file_size")))
        if url.endswith("/media"):
            return FakeResp({"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/C1"})
        if "rupload" in url:
            return FakeResp({"success": True})
        return FakeResp({"id": "M1"})

    def fake_get(url, **kw):
        if url.endswith("/C1"):
            return FakeResp({"status_code": "FINISHED"})
        return FakeResp({"permalink": "https://instagram.com/reel/abc", "media_url": "https://cdn/v.mp4"})

    monkeypatch.setattr(meta_api.requests, "post", fake_post)
    monkeypatch.setattr(meta_api.requests, "get", fake_get)
    r = meta_api.post_instagram(str(video), build_captions(META), {"ig_user_id": "IG", "token_env": "TOK"})
    assert r == {"id": "M1", "url": "https://instagram.com/reel/abc", "media_url": "https://cdn/v.mp4"}
    assert [x[1].split("/")[-1] for x in log] == ["media", "C1", "media_publish"]
    assert log[1][2] == str(os.path.getsize(video))


def test_threads_requires_public_url(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    with pytest.raises(PublishError):
        meta_api.post_threads("x.mp4", build_captions(META), {"user_id": "U", "token_env": "TOK"}, None)


def test_missing_token_env_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(PublishError, match="NOPE"):
        meta_api.post_facebook("x.mp4", build_captions(META), {"page_id": "P", "token_env": "NOPE"})


def test_delete_removes_meta_posts_once(monkeypatch):
    ledger = {"output/ja/cards/ani-004_x.mp4": {
        "instagram": {"id": "i1"}, "facebook": {"id": "f1"}, "threads": {"id": "t1"}, "tiktok": {"id": "manual"}}}
    deleted = []
    monkeypatch.setattr(cli.meta_api, "delete_post", lambda p, pid, cfg: deleted.append((p, pid)))
    cfg = {"instagram": {}, "facebook": {}, "threads": {}}
    assert cli.delete_video("ani-004_x", cfg, ledger, dry_run=False) == ["instagram", "facebook", "threads"]
    assert cli.delete_video("ani-004_x", cfg, ledger, dry_run=False) == []   # already deleted
    assert deleted == [("instagram", "i1"), ("facebook", "f1"), ("threads", "t1")]


def test_default_platforms_are_meta_only():
    assert cli.DEFAULT_PLATFORMS == ["instagram", "facebook", "threads"]


def test_unlicensed_old_clips_are_never_pending(tmp_path):
    card_clip = _make_clip(tmp_path, name="01_card")
    old = tmp_path / "output" / "ja" / "vid" / "02_old.json"
    old.write_text(json.dumps({k: v for k, v in META.items() if k != "mode"}), encoding="utf-8")
    old.with_suffix(".mp4").write_bytes(b"x")
    assert cli.pending_videos(tmp_path / "output", "ja", {}, ["instagram"]) == [card_clip]
