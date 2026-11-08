import datetime as dt
import json
import random

from quote_clips import kotonex_daily as kd

META = {
    "quote": "In the morning when thou risest unwillingly,\nlet this thought be present",
    "speaker": "Marcus Aurelius",
    "cite": "\u2014 Marcus Aurelius Meditations, Book V, 1",
    "description": "\u201c\u2026\u201d\n\u2014 x\n\nSource: y\nMusic: \"Inspired\" by Kevin MacLeod (incompetech.com), CC BY 4.0\n#quotes",
    "hashtags": ["#quotes", "#motivation", "#mindset", "#MarcusAurelius"],
}


def test_posts_have_link_credit_tags_and_short_title():
    p = kd.build_posts(META, is_anime=False)
    assert p["title"].endswith("#Shorts") and len(p["title"]) <= 90 and "\n" not in p["title"]
    assert kd.APP_URL in p["youtube"]["description"] and kd.APP_URL in p["instagram"]["caption"]
    assert "http" not in p["tiktok"]["caption"].replace("https://creativecommons", "")
    assert "link in bio" in p["tiktok"]["caption"] and "link in bio" in p["instagram"]["caption"]
    for text in (p["youtube"]["description"], p["tiktok"]["caption"], p["instagram"]["caption"]):
        assert "Music: \"Inspired\" by Kevin MacLeod" in text
        tags = [w for w in text.split() if w.startswith("#")]
        assert 3 <= len(tags) <= 5 and "#KOTONEX" in tags
    assert p["youtube"]["privacy"] == "public" and p["tiktok"]["privacy_level"] == "PUBLIC_TO_EVERYONE"


def test_anime_posts_carry_not_official_note_and_long_title_is_cut():
    meta = {**META, "quote": "word " * 40}
    p = kd.build_posts(meta, is_anime=True)
    assert len(p["title"]) <= 90 and "…" in p["title"]
    assert kd.ANIME_NOTE in p["instagram"]["caption"]


def test_pick_avoids_used_and_cycles():
    pool = [{"id": "a", "speaker_ja": "A", "scenes": ["study"]}, {"id": "b", "speaker_ja": "B", "scenes": []}]
    used = {"a"}
    assert kd.pick(pool, used, random.Random(1))["id"] == "b"
    used = {"a", "b"}
    assert kd.pick(pool, used, random.Random(1), prefer=("study",))["id"] == "a"
    assert used == set()


def test_run_dry_run_fills_only_future_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(kd, "STATE", tmp_path / "state")
    data = tmp_path / "data"
    data.mkdir()
    en = {"text_en": "Words", "speaker_en": "X", "source_en": "Book"}
    figs = [{"id": f"fig-{i}", "kind": "figure", "text_ja": "言葉", "speaker_ja": f"S{i}", "scenes": [], **en}
            for i in range(10)]
    figs.append({"id": "fig-live", "kind": "figure", "text_ja": "言葉", "speaker_ja": "L", "speaker_living": True, **en})
    figs.append({"id": "fig-noen", "kind": "figure", "text_ja": "言葉", "speaker_ja": "N", "text_en": ""})
    anime = [{"id": "ani-1", "kind": "anime", "text_ja": "短い", "speaker_ja": "C", **en},
             {"id": "ani-long", "kind": "anime", "text_ja": "長" * 80, "speaker_ja": "D", **en}]
    (data / "quotes_figures.json").write_text(json.dumps(figs, ensure_ascii=False))
    (data / "quotes_anime.json").write_text(json.dumps(anime, ensure_ascii=False))
    now = dt.datetime(2026, 11, 8, 13, 0, tzinfo=kd.JST)
    res = kd.run(now, data, dry_run=True)
    assert [r["at"][11:16] for r in res] == ["17:30", "20:00", "22:00"]
    ids = [r["quote_id"] for r in res]
    assert "ani-1" in ids and not {"fig-live", "fig-noen", "ani-long"} & set(ids)
    assert len({r["speaker"] for r in res}) == 3
