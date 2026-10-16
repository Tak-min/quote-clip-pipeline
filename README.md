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

`sources.csv`（1行 = 1本の元動画）:

```csv
url,speaker,category,work
https://www.youtube.com/watch?v=UF8uR6Z6KLc,スティーブ・ジョブズ,偉人,
https://www.youtube.com/watch?v=XXXX,炭治郎,アニメ,鬼滅の刃
```

```bash
.venv/bin/python -m quote_clips.run --sources sources.csv --app-name "アプリ名"
```

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

- `output/ja/<video_id>/NN_<タイトル>.mp4` … 日本語字幕版（日本語アカウント用）
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

## 投稿（TikTok / Instagram / Facebookリール / Threads）

```bash
cp accounts.example.json accounts.json   # 言語ごとに各SNSのアカウントを書く
.venv/bin/python -m quote_clips.publish --lang ja --limit 1 --dry-run   # 何が投稿されるか確認（APIは呼ばない）
.venv/bin/python -m quote_clips.publish --lang ja --limit 1             # 未投稿の古い順に1本、全SNSへ
.venv/bin/python -m quote_clips.publish --lang en --limit 1
```

- `output/posted.json` に投稿結果を記録。再実行しても二重投稿しない。1つのSNSが失敗しても他は続行し、次回そのSNSだけ再挑戦。
- 既定の投稿先は TikTok と Meta系（Instagram / Facebook / Threads）。`accounts.json` に書いていないSNSはスキップ。`--platforms tiktok,instagram` で絞れる。
- YouTubeは既定から外している。使うときは `accounts.json` に `youtube` を足し、`--platforms youtube` を付ける。
- 認証情報は `secrets/`（トークンファイル）と環境変数（`.env`）に置く。どちらもgit管理外。

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
