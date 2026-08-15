"""
YouTube ライブ配信アーカイブのチャットリプレイを取得し、コメント流量グラフ用の
JSON を生成するスクリプト。

Kick 版 (kick_archiver_with_comments_github.py) の構造を踏襲:
  索引読込 → 新規差分検出 → 各動画のチャット取得 → 保存 → 索引更新 → 古いデータ削除

データ源のみ Kick API から YouTube (yt-dlp で列挙 / chat-downloader でチャット取得) に差し替え。
出力フォーマットもフロント (sortcomments/commentgraph.js) がそのまま使えるよう合わせている。
ただしタイムスタンプは「配信開始からの経過秒 offset」で保存する (リプレイの time_in_seconds)。
"""

import json
import os
import time
import functools
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# yt-dlp は requirements.txt でインストール。
# チャットリプレイは yt-dlp の live_chat 字幕機能で取得する
# (chat-downloader は現行 YouTube をパースできず ParsingError になるため不採用)。
from yt_dlp import YoutubeDL

# すべての print() を stderr に出す (Kick 版と同じ。JSON を stdout に混ぜないため)
print = functools.partial(print, file=sys.stderr, flush=True)

# === ユーザーが指定する基準日時 (ここより後の配信のみ対象) ===
USER_START_DATE = "2026-08-01T00:00:00+09:00"

# === 対象チャンネル ===
CHANNELS = [
    {"handle": "mokouliszt",  "channel_id": "UCZFxcWJS1_iVIFETARRRHZQ"},
    {"handle": "mokoustream", "channel_id": "UCENoC6MLc4pL-vehJyzSWmg"},
]

# === 動作パラメータ ===
# 1回の実行で「チャンネルごとに」新規取得する最大本数 (長時間配信の取得負荷対策)。
# チャンネル単位にすることで、先頭チャンネルに未取得が多くても後続チャンネルが枯渇しない。
MAX_NEW_PER_CHANNEL = 3
# /streams タブから拾う最大件数 (新しい順)
STREAMS_SCAN_LIMIT = 80
# コメント1件あたりのテキスト最大長 (肥大化防止)
MAX_TEXT_LEN = 200

# === 保存先 ===
COMMENTS_GITHUB = "comments_github"
COMMENTS_LOCAL = "comments_local"
ARCHIVE_FILE = "youtube_archives.json"
os.makedirs(COMMENTS_GITHUB, exist_ok=True)
os.makedirs(COMMENTS_LOCAL, exist_ok=True)

# cookies の指定 (bot 判定対策)。どちらか一方を使う。
#  - YT_COOKIES_FILE:         Netscape 形式 cookies.txt のパス (GitHub Actions 用: Secret から生成)
#  - YT_COOKIES_FROM_BROWSER: インストール済みブラウザから直接読む (ローカル用)。
#                             例 "firefox" / "chrome" / "edge" / "chrome:Default"
COOKIES_FILE = os.getenv("YT_COOKIES_FILE") or None
COOKIES_FROM_BROWSER = os.getenv("YT_COOKIES_FROM_BROWSER") or None


def get_comment_dir():
    """出力フォルダを決定。

    GitHub Actions 実行時、または公開用に YT_PUBLISH=1 を指定したローカル実行時は
    comments_github (= GitHub Pages で配信するフォルダ) に出力する。
    それ以外 (お試しローカル実行) は comments_local。
    """
    if os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("YT_PUBLISH") == "1":
        return COMMENTS_GITHUB
    return COMMENTS_LOCAL


# === ユーティリティ ===
def format_duration(seconds):
    """秒を HH:MM:SS に整形。"""
    try:
        s = int(seconds)
        return time.strftime("%H:%M:%S", time.gmtime(s))
    except Exception:
        return "00:00:00"


def unix_to_iso(ts):
    """unix 秒 → ISO8601 (UTC)。"""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


# === アーカイブ列挙 (yt-dlp) ===
def _ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "ignoreerrors": True,
        # 我々は動画フォーマットを一切使わない (メタ情報 + live_chat 字幕のみ)。
        # ログイン cookie を渡すと YouTube がフォーマットに PO トークンを要求し
        # "Requested format is not available" でフォーマット選択が失敗するため、
        # フォーマット不在をエラーにせず、メタ/字幕の取得を続行させる。
        "ignore_no_formats_error": True,
    }
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    if COOKIES_FROM_BROWSER:
        # "browser" または "browser:profile" 形式を受け付ける
        name, _, profile = COOKIES_FROM_BROWSER.partition(":")
        opts["cookiesfrombrowser"] = (name.strip(), profile.strip() or None, None, None)
    if extra:
        opts.update(extra)
    return opts


def list_stream_ids(channel_id):
    """チャンネルの /streams タブ (過去ライブ) を flat で列挙し、videoId のリストを返す。新しい順。"""
    url = f"https://www.youtube.com/channel/{channel_id}/streams"
    opts = _ydl_opts({
        "extract_flat": "in_playlist",
        "playlistend": STREAMS_SCAN_LIMIT,
    })
    ids = []
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = (info or {}).get("entries") or []
        for e in entries:
            if not e:
                continue
            vid = e.get("id")
            if vid:
                ids.append(vid)
    except Exception as e:
        print(f"[{channel_id}] streams 列挙エラー: {e}")
    return ids


def fetch_video_meta(video_id):
    """単体動画のメタ情報 (配信開始・長さ・was_live・タイトル) を取得。"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"[{video_id}] メタ取得エラー: {e}")
        return None
    if not info:
        return None

    # 配信開始時刻: was_live は release_timestamp が実際の開始。無ければ timestamp。
    start_unix = info.get("release_timestamp") or info.get("timestamp")
    duration = info.get("duration") or 0
    return {
        "video_id": video_id,
        "title": info.get("title") or "",
        "start_time": unix_to_iso(start_unix),
        "url": url,
        "duration": duration,
        "video_length": format_duration(duration),
        "live_status": info.get("live_status"),  # 'was_live' / 'not_live' / 'is_live' など
    }


# === コメント取得 (yt-dlp live_chat) ===
def _runs_to_text(runs):
    """live chat の message.runs をテキスト化 (絵文字は shortcut/emojiId に変換)。"""
    parts = []
    for r in runs or []:
        if "text" in r:
            parts.append(r["text"])
        elif "emoji" in r:
            emo = r["emoji"]
            sc = emo.get("shortcuts")
            parts.append(sc[0] if sc else emo.get("emojiId", ""))
    return "".join(parts)


def get_chat_comments(video_id):
    """指定動画のチャットリプレイを取得し、[{id, offset, text}, ...] を返す。

    yt-dlp で live_chat 字幕 (<id>.live_chat.json) を一時DLし、
    replayChatItemAction を1行ずつパースする。
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    comments = []
    with tempfile.TemporaryDirectory() as tmp:
        opts = _ydl_opts({
            "writesubtitles": True,
            "subtitleslangs": ["live_chat"],
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
        })
        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e:
            print(f"[{video_id}] チャット取得エラー: {e}")
            return comments

        path = os.path.join(tmp, f"{video_id}.live_chat.json")
        if not os.path.exists(path):
            print(f"[{video_id}] チャットリプレイ無し")
            return comments

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                rca = obj.get("replayChatItemAction")
                if not rca:
                    continue
                try:
                    offset = int(rca.get("videoOffsetTimeMsec", "0")) / 1000.0
                except Exception:
                    continue
                if offset < 0:
                    continue
                for act in rca.get("actions", []):
                    add = act.get("addChatItemAction")
                    if not add:
                        continue
                    r = add.get("item", {}).get("liveChatTextMessageRenderer")
                    if not r:
                        continue
                    text = _runs_to_text(r.get("message", {}).get("runs", []))[:MAX_TEXT_LEN]
                    if not text:
                        continue
                    comments.append({
                        "id": r.get("authorExternalChannelId"),
                        "offset": round(offset, 1),
                        "text": text,
                    })
    return comments


# === ローカル保存管理 ===
def load_local_archives():
    if os.path.exists(ARCHIVE_FILE):
        try:
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_comment_stats(video, comments):
    comment_dir = get_comment_dir()
    if not comments:
        print(f"コメントなし: {video['video_id']}")
        return False
    try:
        data = {
            "video_id": video["video_id"],
            "start_time": video["start_time"],
            "video_length": video["video_length"],
            "number_of_comments": video["number_of_comments"],
            "comments": comments,
        }
        path = os.path.join(comment_dir, f"{video['video_id']}_comments.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print(f"コメント統計保存: {path} ({len(comments)}件)")
        return True
    except Exception as e:
        print(f"統計保存エラー({video['video_id']}): {e}")
        return False


def update_archive_data(archives):
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(archives, f, ensure_ascii=False, indent=2)
    print(f"📁 {ARCHIVE_FILE} 更新完了 ({len(archives)}件)")


def cleanup_old_comments():
    """30日より古いコメントJSONを削除 (GitHub フォルダのみ)。索引からも除去。Kick 版と同じ発想。"""
    limit = datetime.now(timezone.utc) - timedelta(days=30)
    removed_ids = set()

    for el in os.listdir(COMMENTS_GITHUB):
        if not el.endswith("_comments.json"):
            continue
        path = os.path.join(COMMENTS_GITHUB, el)
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            continue
        created = obj.get("start_time")
        if created:
            try:
                ctime = datetime.fromisoformat(created)
            except Exception:
                continue
            if ctime < limit:
                os.remove(path)
                removed_ids.add(obj.get("video_id"))
                print(f"🧹 古いコメント削除: {el}")

    return removed_ids


# === メイン ===
def main():
    try:
        print("YouTube アーカイブ収集を開始...")
        local_archives = load_local_archives()
        known_ids = {a["video_id"] for a in local_archives}
        user_start_dt = datetime.fromisoformat(USER_START_DATE).astimezone(timezone.utc)

        total_new = 0
        for ch in CHANNELS:
            print(f"--- チャンネル: {ch['handle']} ---")
            stream_ids = list_stream_ids(ch["channel_id"])
            print(f"  {len(stream_ids)} 本の配信を検出")

            ch_new = 0  # このチャンネルで今回取得した本数
            for video_id in stream_ids:
                if ch_new >= MAX_NEW_PER_CHANNEL:
                    break
                if video_id in known_ids:
                    continue

                meta = fetch_video_meta(video_id)
                if not meta or not meta["start_time"]:
                    continue

                start_dt = datetime.fromisoformat(meta["start_time"])
                # /streams は新しい順。基準日時より古い動画に到達したら、それ以降は全て古いので打ち切り。
                if start_dt < user_start_dt:
                    print(f"  基準日時より古いため打ち切り: {video_id} ({meta['start_time']})")
                    break
                # /streams タブは過去ライブのみ。まだ配信中/配信予定のものだけ除外し、
                # メンバー限定を含む全アーカイブを対象にする (メンバー限定は member 資格の cookie が必要)。
                if meta.get("live_status") in ("is_live", "is_upcoming"):
                    print(f"  まだ配信中/予定のためスキップ: {video_id} ({meta.get('live_status')})")
                    continue

                print(f"  新規: {meta['title']} ({video_id})")
                comments = get_chat_comments(video_id)
                meta["number_of_comments"] = len(comments)
                meta.pop("live_status", None)

                # コメントがあれば JSON を出力 (無ければファイルは作らない)。
                save_comment_stats(meta, comments)
                # コメント有無に関わらず「処理済み」として索引に記録する。
                # こうしないとチャット無し/アクセス不可の配信を毎回リトライし続け、
                # 枠を専有して古い配信へ進めなくなる (特にメンバー限定)。
                # 後で再取得したい場合は youtube_archives.json から該当エントリを削除すればよい。
                local_archives.append(meta)
                known_ids.add(video_id)
                ch_new += 1
                total_new += 1
                update_archive_data(local_archives)  # 各動画ごとに索引を保存 (途中終了に強く)
                time.sleep(2)

        if total_new == 0:
            print("新しいアーカイブはありません。")

        removed = cleanup_old_comments()
        if removed:
            local_archives = [a for a in local_archives if a["video_id"] not in removed]
            update_archive_data(local_archives)

        print("✨ 完了")

    except Exception as e:
        print(f"実行中エラー: {e}")


if __name__ == "__main__":
    main()
