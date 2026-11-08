"""quote_clips/copywriter.py — On-screen copy and the post caption for one clip, written by the LLM.

Modelled on Shift's motivation shorts (@shiftjp.app / @kachimind_jp):
- headline: the lesson in 2 bold lines above the footage
- context (white) + lesson (yellow) under the footage
- notify: the quote as the app's push notification near the end
- caption: quote → who/when/what happened → reading of the words → lesson → closing line
"""

from typing import Dict

from loguru import logger

from quote_clips.llm import call_llm, parse_json

PROMPT = """あなたはモチベーション系ショート動画（Instagramリール/TikTok）のコピーライターだ。
出力言語: {lang_name}
発言者: {speaker}{work_note}
場面（人が書いた出典。空なら不明）: {cite}
この動画で話している内容（字幕）:
{subs}

書くもの:
1. headline: 画面上部の見出し。この言葉から読み取れる教訓を短く言い切る。{headline_rule}
2. context: 映像の下に白文字で出す2行。1行目は肩書き付きの発言者名（例: 名将ジョゼ・モウリーニョ）。発言者が「（不明）」なら人名を書かず、作品名だけにする（例: アニメ『ハイキュー!!』）。「不明」という語は絶対に書かない。2行目はこの言葉の重みが伝わる実績や状況を1文。
3. lesson: 映像の下に黄色で出す2〜3文。教訓と、視聴者への一言。各文{line_rule}
4. notify: アプリのプッシュ通知に出す一言。この言葉の核を{notify_rule}
5. cite: 出典の場面（例: チェルシー監督就任会見（2004））。上の「場面」が空でなく正しければそのまま。
6. caption: 投稿文の本文。次の順で、段落は空行で区切る:
   (a) 発言者の紹介（誰か・代表的な実績）と、この言葉を言った場面
   (b) その言葉の受け止め方・なぜ刺さるか（2〜3文）
   (c) 視聴者への教訓（2〜3文、短い文で言い切る）
   (d) 締めの一文
   名言そのもの・ハッシュタグ・アプリの宣伝は書かない（別に付ける）。

厳守:
- 事実（年・記録・経歴・場面）は確実に知っていることだけ書く。自信がなければ書かずに、言葉の解釈で埋める。
- 説教くさくしない。です・ます調（日本語）。英語は短い文で力強く。
- JSONのみを返す: {{"headline": "", "context": ["", ""], "lesson": ["", ""], "notify": "", "cite": "", "caption": ""}}
"""

RULES = {
    "ja": {"name": "日本語", "headline": "全角26文字以内で2行。行の切れ目に\\nを1つ入れる（各行13文字以内）。",
           "line": "全角24文字以内。", "notify": "全角20文字以内で。"},
    "en": {"name": "English", "headline": "max 44 characters, 2 lines with one \\n (each line max 24 chars).",
           "line": "max 48 characters.", "notify": "in max 40 characters."},
}


def write_copy(speaker: str, work: str, cite: str, subtitles: str, lang: str) -> Dict:
    """Return {headline, context[], lesson[], notify, cite, caption}; empty fields when the LLM fails."""
    r = RULES[lang]
    prompt = PROMPT.format(
        lang_name=r["name"], speaker=speaker or "（不明）", work_note=f"（作品『{work}』）" if work else "",
        cite=cite or "", subs=subtitles, headline_rule=r["headline"], line_rule=r["line"], notify_rule=r["notify"],
    )
    empty = {"headline": "", "context": [], "lesson": [], "notify": "", "cite": cite, "caption": ""}
    try:
        data = parse_json(call_llm(prompt))
    except Exception as e:
        logger.warning(f"copywriter failed ({speaker}, {lang}): {e}")
        return empty
    if not isinstance(data, dict):
        return empty

    def lines(v: object) -> list:
        return [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x or "").strip()][:3]

    return {
        "headline": str(data.get("headline") or "").replace("\\n", "\n").strip(),
        "context": lines(data.get("context")),
        "lesson": lines(data.get("lesson")),
        "notify": str(data.get("notify") or "").strip(),
        "cite": cite or str(data.get("cite") or "").strip(),
        "caption": str(data.get("caption") or "").strip(),
    }
