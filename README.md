# youtube-comment-fetcher

YouTube のライブ配信アーカイブからチャットリプレイを収集し、コメント流量グラフ用の
JSON を生成・配信するバックエンド。ブラウザ拡張 `sortcomments`（YT コメントソーター）が
この JSON を読み込み、視聴ページにグラフを埋め込む。

Kick 版 [`kick-comment-fetcher`](https://github.com/tourist1159/kick-comment-fetcher) の
YouTube 対応版。

## 対象チャンネル

| ハンドル | channelId |
|---|---|
| [@mokouliszt](https://www.youtube.com/@mokouliszt) | `UCZFxcWJS1_iVIFETARRRHZQ` |
| [@mokoustream](https://www.youtube.com/@mokoustream) | `UCENoC6MLc4pL-vehJyzSWmg` |

`youtube_archiver_with_comments_github.py` の `CHANNELS` を編集すれば増減できる。

## 仕組み

各チャンネルにつき **2つのタブ**を収集する（時系列サイト向けに配信も通常動画も網羅）:

- **`/streams`（過去ライブ）→ `type:"stream"`**: メタ情報＋チャットリプレイを取得。
  チャットは **yt-dlp の live_chat 字幕**で取得し `replayChatItemAction` をパースして
  `{id, offset, text}` に整形（chat-downloader は現行 YouTube を解析できないため不採用）。
  メンバー限定も対象（メンバー資格のある cookie を渡した場合のみ実取得。無ければ自動スキップ）。
- **`/videos`（通常動画）→ `type:"video"`**: **メタ情報のみ**（コメントは取得しない）。

共通処理: 新しい順に列挙し、`USER_START_DATE` より古いものに到達したら打ち切り。
配信中/配信予定は除外。取得したものは索引 `youtube_archives.json` に記録（チャット取得不可の
配信も記録して毎回の再取得を防ぐ）。取得負荷の重い配信は少なめ・通常動画は多めの上限を設定。

> ⚠️ メンバー限定配信のコメント（本文・投稿者ID）も、収集すれば公開の GitHub Pages で
> 誰でも閲覧可能になります。承知の上で運用してください。

生成物:
- `youtube_archives.json` … 収集済みコンテンツの索引（配信＋通常動画）。時系列サイトの主データ。
  各エントリ:
  ```json
  // 配信
  { "video_id":"RSGOhFhym8k", "title":"...", "start_time":"2026-08-14T07:10:20+00:00",
    "url":"https://www.youtube.com/watch?v=RSGOhFhym8k", "duration":33509,
    "video_length":"09:18:29", "type":"stream", "number_of_comments":21935 }
  // 通常動画 (コメントなし)
  { "video_id":"WoC3TpJORaY", "title":"...", "start_time":"2026-08-04T...",
    "url":"...", "duration":451, "video_length":"00:07:31", "type":"video" }
  ```
  拡張は `type:"stream"` のみグラフ対象にする（`type:"video"` は無視）。
- `comments_github/<videoId>_comments.json` … 配信1本ごとのコメント（通常動画には作られない）
  ```json
  {
    "video_id": "RSGOhFhym8k",
    "start_time": "2026-08-14T09:00:00+00:00",
    "video_length": "09:18:29",
    "number_of_comments": 41389,
    "comments": [ { "id": "UCxxxx", "offset": 12.3, "text": "草" } ]
  }
  ```
  `offset` は配信開始からの経過秒。フロントは `Math.floor(offset/60)` で1分バケットに集計する。
  古いコメントファイルは30日で削除されるが、索引エントリ（メタ情報）は時系列サイト用に残す。

## 運用: ローカル実行 + push（推奨・採用中）

GitHub Actions のデータセンターIPは YouTube に bot 判定されやすいため、**取得は自宅PCで実行**し、
結果を GitHub へ push して Pages で配信する構成を採用している（Actions の定期実行は無効化済み）。

### お試し実行

```bash
pip install -r requirements.txt
python youtube_archiver_with_comments_github.py
```

`YT_PUBLISH` も `GITHUB_ACTIONS` も無いと出力は `comments_local/`（配信対象外）。

### 定期実行（`run_and_push.ps1` + タスクスケジューラ）

`run_and_push.ps1` が「yt-dlp更新 → 収集(公開フォルダ出力) → commit → push」を行う。
- 収集は `YT_PUBLISH=1` で `comments_github/` に出力（= Pages 配信対象）。
- リポジトリ直下に `cookies.txt`（メンバー資格アカウントのもの）を置くと、
  メンバー限定アーカイブも取得対象になる。`.gitignore` 済みでコミットされない。
- 1回の実行で取得する新規本数は **チャンネルごとに** `MAX_NEW_PER_CHANNEL`（既定3）に制限。
  追いつくまで少しずつ収集。チャンネル単位なので、先頭チャンネルに未取得が多くても後続チャンネルが枯渇しない。
- 30日より古いコメントは自動削除（`cleanup_old_comments`）。

Windows タスクスケジューラ登録例（1時間ごと）:
- プログラム: `powershell.exe`
- 引数: `-ExecutionPolicy Bypass -NoProfile -File "D:\81801\Documents\YT-extension\youtube-comment-fetcher\run_and_push.ps1"`

### （任意）GitHub Actions を使う場合

`fetch.yml` は定期実行をコメントアウト済み。手動の workflow_dispatch のみ。使うなら Secret
`YT_COOKIES` が必要（データセンターIPでメンバー垢を使うのは凍結リスク高め）。

## 配信（GitHub Pages）

リポジトリの Pages を有効化すると、
`https://<user>.github.io/youtube-comment-fetcher/youtube_archives.json` 等で配信される。
拡張側の取得先 `BASE` はこの URL に合わせる（`sortcomments/commentgraph.js`）。
