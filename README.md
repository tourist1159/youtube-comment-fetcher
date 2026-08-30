# youtube-comment-fetcher

YouTube のライブ配信アーカイブ/通常動画のメタ情報とチャットリプレイを収集し、
コメント流量グラフ・時系列サイト用の JSON を生成・配信するバックエンド。
ブラウザ拡張 `sortcomments`（YT コメントソーター）とまとめサイト `mokou-timeline` が
この JSON を読み込む。

Kick 版 [`kick-comment-fetcher`](https://github.com/tourist1159/kick-comment-fetcher) の
YouTube 対応版。

## 対象チャンネル

| ハンドル | channelId |
|---|---|
| [@mokouliszt](https://www.youtube.com/@mokouliszt) | `UCZFxcWJS1_iVIFETARRRHZQ` |
| [@mokoustream](https://www.youtube.com/@mokoustream) | `UCENoC6MLc4pL-vehJyzSWmg` |

`youtube_meta_fetcher.py` と `youtube_archiver_with_comments_github.py` 両方の
`CHANNELS`（前者は後者と別ファイル）を編集すれば増減できる。

## 仕組み（2系統に分離）

メタ情報収集とチャット取得は、bot判定リスクの違いから**別スクリプト・別実行環境**に分けている。

### ① メタ情報収集（クラウド・GitHub Actions）— `youtube_meta_fetcher.py`
新規動画IDの**列挙**は yt-dlp の flat 抽出（`/streams`・`/videos` タブ一覧、軽量）、
タイトル・開始時刻・長さ・配信/通常動画の**判別**は YouTube **Data API v3**（公式API）
で行うハイブリッド方式。

- yt-dlp で `/streams`・`/videos` タブを新しい順に flat 列挙し、未知の動画IDを収集
  （1件ずつのフル `extract_info` とは異なる軽量な一覧取得のため、GitHub Actions の
  データセンターIPでも bot 判定を受けにくい）
- 列挙だけ yt-dlp を使う理由: **アップロード済みプレイリスト（Data APIが見る場所）への
  反映には配信終了から数十分〜数時間のラグがあるが、`/streams` タブは先に更新される**
  ことを実測で確認したため（2026-08-19）。フォールバックとして、yt-dlp列挙で新規が
  0件のときのみ Data API の `channels.list`→`playlistItems.list` でも追加確認する
- 新規動画IDを `videos.list` でバッチ取得し、`liveStreamingDetails` の有無で
  `type:"stream"/"video"` を判別（配信中でまだ終了していないものは除外し、後日改めて拾う）
- `youtube_archives.json` に追記。**`number_of_comments` は付与しない**
  （②のローカルジョブが「チャット未取得」を判定する目印として使うため）
- **メンバー限定 `members_only`**: flat 抽出の `availability == "subscriber_only"`（サムネの
  「メンバー限定」バッジ由来）で判別する。Data API 側にこれを直接示すフィールドは無く、
  **メンバー限定動画は `statistics` 自体が返らない**（＝再生数も取得できない）。列挙は毎回
  行っているので、新規収集時だけでなく毎回付け直す（後からメンバー限定を解除する運用に追従）。
  列挙範囲（`SCAN_LIMIT` 件）より古いエントリのフラグは触らない
- **再生数 `view_count`**: `videos.list` の part に `statistics` を足して取得する。
  パーツを増やしてもコストは変わらない（videos.list は1回の呼び出しにつき1ユニット、50件まで）。
  新規動画は収集時に入るが、既存動画の数字は古くなるので**12時間ごとに全件を取り直す**
  （`VIEW_REFRESH_SECONDS`）。毎時の実行のたびに全件書き換えると `youtube_archives.json` が
  毎時コミットされ、そのたびに GitHub Pages の再ビルドが走ってしまうため
  （mokou-timeline 側で同じことをして account 全体の cron を間引かれた前例がある）。
  最後に更新した時刻は `meta_state.json` に持つ（`youtube_archives.json` は配列で、
  サイト側がそのまま読むため付帯情報を混ぜられない）。42件なら1回1ユニット＝1日2ユニット
- `.github/workflows/meta-fetch.yml` で毎時実行。Secret `YOUTUBE_API_KEY` が必要
  （Google Cloud Console で YouTube Data API v3 を有効化し取得。無料枠1日10,000unitに対し
  消費は数十〜百unit程度で余裕）。

### ② チャット取得（ローカル）— `youtube_archiver_with_comments_github.py`
①が書き込んだ索引から、`type:"stream"` かつ `number_of_comments` 未設定（＝チャット未取得）の
エントリだけを対象に、**yt-dlp の live_chat 字幕機能**でチャットリプレイを取得する
（chat-downloader は現行 YouTube を解析できないため不採用）。cookie 必須の処理
（メンバー限定チャンネル対応、bot判定回避）のため、**自宅PCでのローカル実行**が前提。

- `replayChatItemAction` をパースして `{id, offset, text}` に整形
- メンバー限定も対象（メンバー資格のある cookie を渡した場合のみ実取得。無ければ自動スキップ）
- 取得後は成功/失敗（0件）に関わらず `number_of_comments` をセットし、再取得を防ぐ
- チャンネルごとに新しい順で最大 `MAX_NEW_STREAMS_PER_CHANNEL`（既定3）件/回に制限
  （取得負荷が重いため。枯渇防止でチャンネル単位）

> ⚠️ メンバー限定配信のコメント（本文・投稿者ID）も、収集すれば公開の GitHub Pages で
> 誰でも閲覧可能になります。承知の上で運用してください。

生成物:
- `youtube_archives.json` … 収集済みコンテンツの索引（配信＋通常動画）。時系列サイトの主データ。
  **`start_time` の新しい順（降順）にソート**して書き出す。各エントリ:
  ```json
  // 配信 (①②とも実行済み)
  { "video_id":"RSGOhFhym8k", "title":"...", "start_time":"2026-08-14T07:10:20+00:00",
    "url":"https://www.youtube.com/watch?v=RSGOhFhym8k", "duration":33509,
    "video_length":"09:18:29", "type":"stream", "channel":"mokouliszt",
    "view_count":123456, "number_of_comments":21935 }
  // 通常動画 (①のみ。コメントなし)
  { "video_id":"WoC3TpJORaY", "title":"...", "start_time":"2026-08-04T...",
    "url":"...", "duration":451, "video_length":"00:07:31", "type":"video",
    "channel":"mokouliszt", "view_count":98765 }
  // メンバー限定配信 (再生数は YouTube が公開していないため view_count が付かない)
  { "video_id":"...", "type":"stream", "channel":"mokouliszt", "members_only":true, ... }
  // 配信だが②未実行 (number_of_comments が無い = チャット取得待ち)
  { "video_id":"...", "type":"stream", "channel":"mokoustream", ... }
  ```
  拡張/サイトは `type:"stream"` のみグラフ対象にする（`type:"video"` は無視）。
  `view_count` は12時間ごとに更新される概数で、視聴回数を非公開にしている動画では付かない。
- `meta_state.json` … 再生数を最後に一括更新した時刻だけを持つ小さなファイル
  （`{"view_counts_updated_at":"..."}`）。サイトは読まない。
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

## セットアップ

### ①メタ収集（GitHub Actions）
1. Google Cloud Console でプロジェクトを作成し「YouTube Data API v3」を有効化、APIキーを発行。
2. リポジトリの Settings → Secrets → Actions に `YOUTUBE_API_KEY` として登録。
3. Actions で `YouTube Meta Fetcher (Data API v3)` ワークフローが毎時自動実行される。

### ②チャット取得（ローカル + タスクスケジューラ）
```bash
pip install -r requirements.txt
python youtube_archiver_with_comments_github.py
```
`YT_PUBLISH` も `GITHUB_ACTIONS` も無いと出力は `comments_local/`（配信対象外）。

`run_and_push.ps1` が「git pull → yt-dlp更新 → チャット取得(公開フォルダ出力) → commit → push」
を行う。実行前に必ず `git pull --rebase` する（① Actions が並行して新規エントリを push する
ため、最新の索引を見てから「未取得」を判定する必要がある）。

- リポジトリ直下に `cookies.txt`（メンバー資格アカウントのもの）を置くと、
  メンバー限定アーカイブも取得対象になる。`.gitignore` 済みでコミットされない。

Windows タスクスケジューラ登録例（1時間ごと）:
- プログラム: `powershell.exe`
- 引数: `-ExecutionPolicy Bypass -NoProfile -File "D:\81801\Documents\YT-extension\youtube-comment-fetcher\run_and_push.ps1"`

### （緊急時フォールバック）
`fetch.yml` は旧・統合版（yt-dlp でメタ+チャット両方）で、手動実行のみ。ローカルPCが使えない
緊急時用。データセンターIPで bot 判定されやすいため、使うなら Secret `YT_COOKIES` を推奨。

## 配信（GitHub Pages）

リポジトリの Pages を有効化すると、
`https://<user>.github.io/youtube-comment-fetcher/youtube_archives.json` 等で配信される。
拡張側の取得先 `BASE` はこの URL に合わせる（`sortcomments/commentgraph.js`）。
