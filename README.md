# quote-clip-pipeline

名言切り抜きショートを自動生成するパイプライン。
`~/youtube-shorts-factory-product/clip_pipeline` をベースにリミックスしたもの（元のリポジトリは変更していない）。

```
sources.csv → yt-dlp → faster-whisper(文単位) → Groqで名言区間を選定
  → 1080x1920 縦型（16:9を中央に、背景ぼかし）
  → 上部: 引きの一言 / 中央下: 日本語字幕（日本語以外はLLM翻訳） / 下部: 発言者テロップ
  → BGM → 末尾1.5秒のアプリ名カード → ラウドネス正規化
```

## セットアップ

```bash
uv venv --python python3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env   # 必要ならLLMの優先順・モデル・キー保存先を上書き
```

ffmpeg が必要（`brew install ffmpeg`）。日本語フォントは macOS のヒラギノを自動使用。他環境は `QUOTE_FONT` で指定。

## 使い方

**基本は名言テキストカード動画**（他人の映像・音声を使わない）。切り抜きは、権利者が再利用を明示的に許可した素材だけ。
運用ルールの根拠は [`research/copyright/DECISION.md`](research/copyright/DECISION.md)。

`sources.csv`（1行 = 1本）:

```csv
mode,quote_id,quote,quote_en,speaker,speaker_en,category,work,work_en,cite,cite_en,url,license,author
card,fig-034,,,,,,,,,,,,
card,,負けは弱さの証明ではない,,武田一鉄,,アニメ,ハイキュー!!,,42巻 第369話,,,,
clip,,,,演説者名,,偉人,,,,,https://...,CC BY 4.0,権利者名
```

- `mode` が空なら `card` として扱う（列の書き忘れで他人の映像が出る事故を防ぐ）。
- `card` 行: `quote_id` を書くとアプリのデータ（`--quotes-data`、既定 `~/meigen-app/data`）から日英の本文・発言者・作品名・出典を読む。直接 `quote` / `speaker` / `cite` を書いてもよい。足りない言語はLLMで翻訳。出典は**文字だけ**（URLは入れない）。
- `clip` 行: `license` が空の行は作らない。投稿文には元動画URL・作者・ライセンスが入る。
- 存命の人物は使わない（本人がアプリを推薦しているように見えるため）。

```bash
.venv/bin/python -m quote_clips.run --sources sources.csv --app-name "言霊" --app-name-en "KOTODAMA"
```

カードの見た目はアプリのシェア画像に合わせている（黒地・Emberの斜線・発言者名が主役）。地の色・斜線の位置・表示位置・出し方は1本ごとに少し変わる。

| オプション | 既定 | 説明 |
|---|---|---|
| `--app-name` | `$APP_NAME` | 末尾カードのアプリ名（空ならカードなし） |
| `--app-name-en` | `$APP_NAME_EN` | 英語版のアプリ名（空なら `--app-name`） |
| `--langs` | ja,en | 作る字幕言語 |
| `--app-tagline` | `$APP_TAGLINE` | アプリ名の下の小さい一文 |
| `--max-clips` | 3 | 元動画1本あたりの最大クリップ数 |
| `--model-size` | turbo | Whisperモデル。M1 CPUで3分の音声に約2分。`small`は速いが誤認識が多い、`large-v3`は約7倍遅い |
| `--layout` | fit | `fit`=全体を中央に表示 / `crop`=中央を切り抜き |
| `--select-only` | - | 名言区間の選定だけ行い、`candidates.json` を確認する |
| `--no-bgm` | - | BGMなし |

出力（アカウントごとに言語別フォルダ）:

- `output/ja/cards/<quote_id>_<発言者>.mp4` … カード（日本語アカウント用）。英語は `output/en/cards/`
- `output/ja/<video_id>/NN_<タイトル>.mp4` … 許可素材の切り抜き（日本語字幕版）
- `output/en/<video_id>/NN_<title>.mp4` … 英語字幕版（英語アカウント用）
- 各 `.json` に投稿用タイトル・説明文・ハッシュタグ・出典URL・BGMクレジット・表示した字幕
- `output/_candidates/<video_id>.json` … 選ばれた名言区間

字幕は単語のタイムスタンプから「文・節の切れ目」で1枚ずつ作り、LLMが意味の切れ目に改行を入れる
（日本語は1行14字、英語は1行30字目安。単語や文節の途中では切らない）。

`--langs ja` のように片方だけ作ることもできる。英語版のアプリ名は `--app-name-en`。
英語の発言者名・作品名は `sources.csv` の `speaker_en` / `work_en` 列で指定でき、空ならLLMが公式英語名に変換する。

## メモ

- **発言者欄**: 1人が話す動画なら名前を書く。複数キャラの名言集なら空欄にする（テロップは作品名だけになる）。発言者を推測で付けることはしない。
- **レート制限の待機**: あるプロバイダの全キーが429/5xxになったら、`Retry-After`（なければ5→10→20秒）だけ待って同じプロバイダを最大3回やり直す。待ち時間が60秒を超える（日次上限など）ときは待たずに次のプロバイダへ。`LLM_RETRY_ROUNDS` / `LLM_BACKOFF_BASE` / `LLM_MAX_WAIT` で調整。
- **LLMの順序**: Groq（`gpt-oss-120b` → `qwen3.8-27b`）を主系にし、Motivation → Daily Curiosity → Animal Microdrama の3キーを順番に使う。全Groq候補が失敗したときだけGeminiの複数キー・モデルへフォールバックする。
- **キー保存先**: 既定では既存の `~/youtube-ai-pipeline/docker/.env` を読む。別の場所は `LLM_ENV_FILE` で指定でき、プロセス環境変数が最優先。
- **Gemini枠**: 現在の処理はすべてテキスト/JSONなのでGemini専用工程はない。将来の画像・動画理解などに備え、通常運用では最終フォールバックとして温存する。
- **字幕**: 日本語音声はLLMで聞き間違いだけを校正、外国語は翻訳。BGMの歌詞と判断した行は消す。
- **元動画に焼き込み字幕がある場合**は二重に出る。気になる素材は `--select-only` で確認してから外す。
- **画質**: 480p未満の元動画は警告が出る（拡大でぼやけるため）。

- 文字起こしは `work/` にキャッシュされる（同じURL・同じモデルは再実行しない）。
- 同梱BGM（Kevin MacLeod, CC BY 4.0）はクレジット表記が必要。`.json` の説明文に自動で入る。

## 投稿（Instagram / Facebookリール / Threads は自動、TikTok は手動）

```bash
cp accounts.example.json accounts.json   # 言語ごとにMeta系のアカウントを書く
.venv/bin/python -m quote_clips.publish --lang ja --limit 1 --dry-run   # 何が投稿されるか確認（APIは呼ばない）
.venv/bin/python -m quote_clips.publish --lang ja --limit 1             # 未投稿の古い順に1本、Meta系へ
.venv/bin/python -m quote_clips.publish --lang ja --platforms tiktok --limit 3 --dry-run   # TikTok用の説明文を表示
.venv/bin/python -m quote_clips.publish delete --lang ja --video ani-004_武田一鉄           # 削除申し立て時: Meta系から消す
```

- `output/posted.json` に投稿結果を記録。再実行しても二重投稿しない。1つのSNSが失敗しても他は続行し、次回そのSNSだけ再挑戦。
- **投稿されるのはカード動画と許可素材の切り抜きだけ**。それ以前に作った切り抜き（記録に `mode` が無いもの）は投稿対象から外れる。
- **TikTok はアプリから手動で投稿**する（Content Posting API の監査は自社アカウント向けの投稿ツールを通さないため）。説明文は上の `--dry-run` で出す。
- 削除申し立てが来たら当日中に `delete` で Meta系から消し、TikTok はアプリで消す。手順は DECISION.md ③。
- 認証情報は `secrets/`（トークンファイル）と環境変数（`.env`）に置く。どちらもgit管理外。
- Meta の開発者アプリ・ビジネスは、アプリ本体の広告・ログインに使うものとは別に作る（DECISION.md ③）。

| SNS | 方式 | 準備 |
|---|---|---|
| YouTube | Data API `videos.insert` | Google CloudでOAuthクライアント（デスクトップ）を作り `secrets/youtube_client_secret.json` に置く → `python -m quote_clips.publish auth youtube --lang ja` で初回だけブラウザ認証（言語ごとに別チャンネルでログイン） |
| TikTok | Content Posting API（Direct Post, ファイル分割アップロード） | 開発者アプリで `video.publish` を取得。`TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` を `.env` に、`{"access_token","refresh_token","expires_at"}` を `secrets/tiktok_<lang>.json` に。**TikTokの監査を通るまでは「自分のみ」公開でしか投稿できない** |
| Instagram | Graph API リール（resumable upload でローカルファイルを直接送る） | プロ（ビジネス/クリエイター）アカウント＋Facebookページ連携、Facebookログイン for Business のアプリ。`ig_user_id` と長期トークン（環境変数名を `token_env` に） |
| Facebookリール | Page `video_reels`（start → upload → finish） | ページの `page_id` とページアクセストークン（`pages_manage_posts` 等） |
| Threads | Threads API（公開URLから取得させる方式のみ） | Threadsのユーザートークン。動画は**同じ回に投稿したInstagramリールの公開URLを再利用**する（ファイル置き場不要）。自前の公開URLがあれば `--public-base-url` |

**Metaの自動シェアについて**: アカウントセンターの「プロフィール間のシェア」でInstagramリールをFacebookへ自動シェアできるが、
API投稿にも適用されるか・Facebookで「リール」扱いになるかは公式に保証されていない。最初の1本で確認し、自動で出るなら
`accounts.json` から `facebook` / `threads` を消せばその分は投稿しない。
