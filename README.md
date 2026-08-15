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

1. 各チャンネルの `/streams`（過去ライブ）を **yt-dlp** で新しい順に列挙。
2. 既知でない動画のメタ（配信開始・長さ・was_live）を yt-dlp で取得。
   `USER_START_DATE` より古い動画に到達したら打ち切り。メンバー限定/非公開は自動スキップ。
3. ライブアーカイブのチャットリプレイを **yt-dlp の live_chat 字幕**で取得し、
   `replayChatItemAction` をパースして `{id, offset, text}` に整形。
   （chat-downloader は現行 YouTube をパースできないため不採用）
4. 結果を保存し、索引 `youtube_archives.json` を更新。

生成物:
- `youtube_archives.json` … 収集済み動画の索引（拡張が videoId の存在確認に使う）
- `comments_github/<videoId>_comments.json` … 1配信ごとのコメント
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

## ローカル実行

```bash
pip install -r requirements.txt
python youtube_archiver_with_comments_github.py
```

`GITHUB_ACTIONS` 環境変数が無い場合、出力は `comments_local/` に保存される
（GitHub Actions 実行時は `comments_github/`）。

## 自動実行（GitHub Actions）

`.github/workflows/fetch.yml` が毎時実行。手動実行は Actions タブの
"Run workflow"（workflow_dispatch）。

- 1回の実行で取得する新規本数は `MAX_NEW_PER_RUN`（既定3）に制限。長時間配信の
  取得負荷とジョブ時間上限に配慮。追いつくまで毎時少しずつ収集する。
- 30日より古いコメントは自動削除（`cleanup_old_comments`）。
- **bot 判定対策**: Actions runner（データセンターIP）が YouTube に弾かれる場合は、
  ブラウザから書き出した cookies を Secrets `YT_COOKIES` に登録し、`fetch.yml` の
  cookies 行と `YT_COOKIES_FILE` を有効化する。

## 配信（GitHub Pages）

リポジトリの Pages を有効化すると、
`https://<user>.github.io/youtube-comment-fetcher/youtube_archives.json` 等で配信される。
拡張側の取得先 `BASE` はこの URL に合わせる（`sortcomments/commentgraph.js`）。
