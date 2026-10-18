# Council 第2回 ブリーフ（自己完結）

## 対象
`~/quote-clip-pipeline`: YouTube等の動画を切り抜き→字幕→縦型ショート→TikTok/Meta系（Instagram・Facebookリール・Threads）へ自動投稿するパイプライン。名言アプリ（偉人＋アニメの名言、iOS、年額課金）の集客・市場テスト用。

## ユーザーの確定事項（覆さない。ただしリスクとして評価する）
- 素材の権利判定をパイプラインに組み込まない（最も簡単な形）
- 偉人＋アニメの名言。日本語・英語の2アカウント。末尾1〜2秒にアプリ名だけ。リンクなし
- 投稿先は TikTok と Meta系
- 出典URL・BGMクレジット（CC BY 4.0）を残すか消すかは未決 → 今回決める

## 今回の主張
`~/quote-clip-pipeline/research/copyright/DECISION.md`（v5草案）全体。要旨: 「無許諾の切り抜きはやめ、許可が明示された偉人講演・米国政府PDだけ切り抜く。アニメ・映画・TV・TED は映像を使わない名言テキストカード動画にする。TikTokは手動投稿、Metaは自動。削除が来たら即削除・対抗通知なし・その言語の切り抜き停止。BGMクレジットは残し、出典URLはカードでは入れない／許可切り抜きでは入れる」

## 根拠
- 経緯と一次情報の一覧: `~/quote-clip-pipeline/research/copyright/loops.md`（第1〜4周。各 Claim に URL）
- 第1回 council の結果: `~/quote-clip-pipeline/research/copyright/council/round1_seats.md`
- パイプライン仕様: `~/quote-clip-pipeline/README.md`、投稿文の生成: `~/quote-clip-pipeline/quote_clips/run.py` の build_meta()
- ペルソナ: `~/meigen-app/docs/persona.md`
- 市場テスト設計: `~/youtube-shorts-factory-product/research/market_test_loops.md`

## 席への依頼
DECISION.md を、あなたの席の視点で批判してください。日本語で:
1. この運用で**まだ残っている最大のリスク**（最大3点）
2. **確定事項に反している／反しかねない**箇所があれば指摘（例: テキストカードは「最も簡単な形」に反するか）
3. **リターンを不必要に捨てている**箇所（最大2点）
4. DECISION の①〜⑤それぞれに「そのまま / 修正（具体的に）」
5. 判定: DECISION を「採用 / 修正して採用 / 棄却」
法的な断定はしないこと。計測・分析基盤の提案はしないこと。500〜800字程度。
