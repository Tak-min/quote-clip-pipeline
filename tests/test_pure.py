"""Unit tests for the pure (no network / no ffmpeg) parts of the pipeline."""

from quote_clips.gemini import parse_json
from quote_clips.render import _font, layout_lines, text_positions, wrap_text
from quote_clips.run import build_meta, chunk_words, clip_subtitles, split_cards, telop_text
from quote_clips.selector import _dedupe, _snap

SEGS = [
    {"text": "a", "start": 0.0, "end": 4.0},
    {"text": "b", "start": 4.0, "end": 9.0},
    {"text": "c", "start": 9.0, "end": 15.0},
    {"text": "d", "start": 15.0, "end": 80.0},
]


def test_wrap_keeps_latin_words_whole():
    lines = wrap_text("Stay hungry, stay foolish forever and ever", _font(64), 400)
    assert all(not ln.endswith(("hung", "fooli")) for ln in lines)
    assert " ".join(lines).replace("  ", " ").startswith("Stay hungry,")


def test_wrap_no_line_starts_with_punctuation():
    lines = wrap_text("あいうえおかきくけこさしすせそ、たちつてと。", _font(64), 64 * 15)
    assert all(not ln.startswith(("、", "。")) for ln in lines)


def test_layout_keeps_explicit_breaks_and_shrinks_instead_of_wrapping():
    lines, font = layout_lines("未来を見て、\n点と点をつなぐことはできない。", 64, 950, 3)
    assert lines == ["未来を見て、", "点と点をつなぐことはできない。"]
    long_line = "点と点をつなぐことは絶対にできないのだ"
    lines, font = layout_lines(long_line, 64, 950, 3)
    assert lines == [long_line] and font.size < 64


def test_fallback_wrap_breaks_after_particle_not_mid_word():
    lines = wrap_text("あなたの人生を変えるのはあなた自身の決断だけなのです", _font(64), 64 * 12)
    assert len(lines) >= 2
    assert all(ln[-1] in "、。はがをにでともへてねよの" or ln == lines[-1] for ln in lines)


def test_text_positions_fit_and_fullframe():
    p = text_positions((656, 1264))
    assert p["hook_center"] < p["telop_top"] < 656 < 1264 < p["sub_top"]
    assert text_positions((0, 1920))["sub_top"] == 1300


def test_clip_subtitles_rezero_and_clamp():
    subs = clip_subtitles(SEGS, 4.0, 15.0)
    assert [s["text"] for s in subs] == ["b", "c"]
    assert subs[0]["start"] == 0.0 and subs[-1]["end"] == 11.0


def test_telop_text():
    assert telop_text("炭治郎", "アニメ", "鬼滅の刃") == "炭治郎（鬼滅の刃）"
    assert telop_text("ジョブズ", "偉人", "") == "ジョブズ"


def test_snap_converts_indices_and_checks_duration():
    c = _snap({"start_index": 1, "end_index": 2, "score": 7, "hook_ja": "x"}, SEGS)
    assert c and c["start"] == 4.0 and c["end"] == 15.0 and c["score"] == 7
    assert _snap({"start_index": 0, "end_index": 0}, SEGS) is None      # 4s: too short
    assert _snap({"start_index": 3, "end_index": 3}, SEGS) is None      # 65s: too long
    assert _snap({"start_index": "x"}, SEGS) is None


def test_dedupe_drops_overlaps():
    kept = _dedupe([{"start": 0, "end": 30, "score": 9}, {"start": 10, "end": 40, "score": 5}, {"start": 40, "end": 60, "score": 4}])
    assert [k["score"] for k in kept] == [9, 4]


def test_parse_json_repairs_fences_and_trailing_commas():
    assert parse_json('```json\n[{"a": 1},]\n```') == [{"a": 1}]
    assert parse_json("note: [1, 2]") == [1, 2]
    assert parse_json("no json") is None


def test_telop_blank_speaker_shows_work_only():
    assert telop_text("", "アニメ", "ハイキュー!!") == "『ハイキュー!!』"
    assert telop_text("", "偉人", "") == ""


def test_telop_english():
    assert telop_text("Tanjiro", "アニメ", "Demon Slayer", "en") == "Tanjiro (Demon Slayer)"
    assert telop_text("", "アニメ", "Haikyu!!", "en") == "Haikyu!!"


def test_split_cards_keeps_two_lines_and_shares_time():
    subs = [{"text": "a\nbb\ncc\nd", "start": 0.0, "end": 6.0}, {"text": "x", "start": 6.0, "end": 7.0}]
    cards = split_cards(subs)
    assert [c["text"] for c in cards] == ["a\nbb", "cc\nd", "x"]
    assert cards[0]["start"] == 0.0 and cards[1]["end"] == 6.0
    assert cards[0]["end"] == cards[1]["start"]


def test_build_meta_per_language():
    cand = {"start": 1.0, "end": 20.0, "quote_ja": "信じろ", "quote_en": "Trust", "title_ja": "題", "title_en": "Title"}
    ja = build_meta("ja", {"speaker": "ジョブズ", "work": ""}, "偉人", cand, "u", "")
    en = build_meta("en", {"speaker": "Steve Jobs", "work": ""}, "偉人", cand, "u", "")
    assert ja["title"] == "題" and "#名言" in ja["hashtags"] and "「信じろ」" in ja["description"]
    assert en["title"] == "Title" and "#SteveJobs" in en["hashtags"] and "Source: u" in en["description"]


def _words(text, t0=10.0, step=0.3, gaps=None):
    out, t = [], t0
    for i, tok in enumerate(text.split()):
        out.append({"text": tok, "start": round(t, 2), "end": round(t + step * 0.9, 2)})
        t += step + (gaps or {}).get(i, 0.0)
    return out


def test_chunk_words_ends_cards_at_sentences_not_segment_cuts():
    ws = _words("Your work is going to fill a large part of your life. And the only way to be truly satisfied "
                "is to do what you believe is great work.")
    cards = chunk_words(ws, 10.0, 40.0, "en")
    assert [c["text"] for c in cards][0] == "Your work is going to fill a large part of your life."
    assert all(c["text"].endswith((".", ",")) or len(c["text"]) >= 80 or c is cards[-1] for c in cards)
    assert cards[0]["start"] == 0.0


def test_chunk_words_japanese_breaks_on_pause():
    ws = [{"text": t, "start": s, "end": s + 0.25} for t, s in
          [("竹", 0.0), ("に", 0.3), ("なろう", 0.6), ("よ", 0.9), ("台風", 2.0), ("が", 2.3), ("来て", 2.6), ("も", 2.9)]]
    cards = chunk_words(ws, 0.0, 5.0, "ja")
    assert [c["text"] for c in cards] == ["竹になろうよ", "台風が来ても"]


def test_cards_from_indices_validates_and_times():
    from quote_clips.translate import cards_from_indices
    ws = [{"text": t, "start": i * 1.0, "end": i * 1.0 + 0.8} for i, t in enumerate("a b c d e".split())]
    cards = cards_from_indices([{"from": 0, "to": 1, "text": "A\\nB"}, {"from": 3, "to": 4, "text": "DE"}], ws)
    assert cards == [{"text": "A\nB", "start": 0.0, "end": 1.8}, {"text": "DE", "start": 3.0, "end": 4.8}]
    assert cards_from_indices([{"from": 2, "to": 3, "text": "x"}, {"from": 3, "to": 4, "text": "y"}], ws) is None
    assert cards_from_indices([{"from": 0, "to": 9, "text": "x"}], ws) is None
    assert cards_from_indices("nope", ws) is None
