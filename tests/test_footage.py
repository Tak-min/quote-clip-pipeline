import json

from quote_clips import footage, kotonex_daily as kd


def test_quote_cards_break_at_natural_points():
    cards = footage.quote_cards("Don't look down! Volleyball is a sport where you always look UP!")
    assert cards == ["Don't look down!", "Volleyball is a sport", "where you always look UP!"]
    long = footage.quote_cards("In the morning when thou risest unwillingly, let this thought be present,"
                               "--I am rising to the work of a human being.")
    assert all(len(c) <= footage.MAX_CARD_CHARS for c in long)
    assert not any(c.split()[-1].lower() in ("the", "a", "of", "to", "be", "you") for c in long)
    assert " ".join(long).replace(" —", "").count("human being.") == 1


def test_card_timings_are_contiguous_and_readable():
    subs = footage.card_timings(["one two three", "four five six seven", "eight"])
    assert subs[0]["start"] == 0.0
    for a, b in zip(subs, subs[1:]):
        assert a["end"] == b["start"]
    assert all(s["end"] - s["start"] >= footage.MIN_SHOT_SEC for s in subs)


def test_app_media_only_uses_reusable_licences(tmp_path):
    data, res = tmp_path / "data", tmp_path / "Resources"
    (res / "Portraits").mkdir(parents=True)
    data.mkdir()
    (res / "Portraits" / "a.jpg").write_bytes(b"x")
    (res / "Portraits" / "b.jpg").write_bytes(b"x")
    (data / "portraits.json").write_text(json.dumps({
        "Free": {"file": "a", "license": "Public domain", "artist": "X"},
        "ShareAlike": {"file": "b", "license": "CC BY-SA 3.0", "artist": "Y"},
    }))
    assert footage.app_media({"id": "q", "speaker_en": "Free"}, res, data)["kind"] == "image"
    assert footage.app_media({"id": "q", "speaker_en": "ShareAlike"}, res, data) is None


def test_posts_carry_footage_credit():
    meta = {"quote": "Be good.", "speaker": "M", "cite": "— M",
            "description": "x\nMusic: \"Inspired\" by Kevin MacLeod\nFootage: Jane (Pexels)\n#quotes",
            "hashtags": ["#quotes", "#motivation"]}
    p = kd.build_posts(meta, is_anime=False)
    for text in (p["youtube"]["description"], p["tiktok"]["caption"], p["instagram"]["caption"]):
        assert "Footage: Jane (Pexels)" in text and "Music:" in text


def test_render_falls_back_to_card(monkeypatch, tmp_path):
    calls = []

    def fake(qid, out_dir, mode):
        calls.append(mode)
        return None if mode == "scene" else tmp_path / f"{qid}.mp4"

    monkeypatch.setattr(kd, "_render_mode", fake)
    assert kd.render("fig-1", tmp_path).name == "fig-1.mp4"
    assert calls == ["scene", "card"]
