"""
YouTube ライブ配信アーカイブのチャットリプレイを取得し、コメント流量グラフ用の
JSON を生成するスクリプト（ローカル専用）。

動画/配信の「メタ情報」収集 (タイトル・開始時刻・長さ・配信/動画の判別) は
youtube_meta_fetcher.py が YouTube Data API v3 で GitHub Actions 上で行う。
このスクリプトは youtube_archives.json を読み込み、まだチャットを取得していない
配信 (type=="stream" かつ number_of_comments 未設定) だけを対象に、
yt-dlp の live_chat 字幕機能でチャットリプレイを取得する。
cookie 必須の処理 (メンバー限定チャンネル等) のため、bot 判定を避けやすい
自宅IPでのローカル実行を前提にしている (chat-downloader は現行 YouTube を
パースできず ParsingError になるため不採用)。

運用: run_and_push.ps1 + タスクスケジューラでこのスクリプトを定期実行し、
結果を GitHub へ push する。GitHub Actions 側 (meta-fetch.yml) が並行して
新規エントリを追加するため、実行前に必ず `git pull --rebase` すること
(run_and_push.ps1 で対応済み)。
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

# すべての print() を stderr に出す (JSON を stdout に混ぜないため)
print = functools.partial(print, file=sys.stderr, flush=True)

# === 動作パラメータ ===
# 1回の実行で「チャンネルごとに」新規取得する最大本数 (チャットDLは重いので少なめ)。
# チャンネル単位にすることで、一方のチャンネルに未取得が多くても他方が枯渇しない。
MAX_NEW_STREAMS_PER_CHANNEL = 3
# コメント1件あたりのテキスト最大長 (肥大化防止)
MAX_TEXT_LEN = 200

# === 保存先 ===
COMMENTS_GITHUB = "comments_github"
COMMENTS_LOCAL = "comments_local"
ARCHIVE_FILE = "youtube_archives.json"
os.makedirs(COMMENTS_GITHUB, exist_ok=True)
os.makedirs(COMMENTS_LOCAL, exist_ok=True)

# cookies の指定 (bot 判定対策 / メンバー限定チャンネル対応)。どちらか一方を使う。
#  - YT_COOKIES_FILE:         Netscape 形式 cookies.txt のパス
#  - YT_COOKIES_FROM_BROWSER: インストール済みブラウザから直接読む。
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


# === yt-dlp オプション ===
def _ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "ignoreerrors": True,
        # 動画フォーマットは一切使わない (live_chat 字幕のみ)。ログイン cookie を渡すと
        # YouTube がフォーマットに PO トークンを要求し "Requested format is not
        # available" でフォーマット選択が失敗するため、それをエラーにせず続行させる。
        "ignore_no_formats_error": True,
    }
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    if COOKIES_FROM_BROWSER:
        name, _, profile = COOKIES_FROM_BROWSER.partition(":")
        opts["cookiesfrombrowser"] = (name.strip(), profile.strip() or None, None, None)
    if extra:
        opts.update(extra)
    return opts


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


def _start_key(a):
    """start_time でソートするためのキー (パース失敗時は最古扱い)。"""
    try:
        return datetime.fromisoformat(a.get("start_time"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def update_archive_data(archives):
    # 時系列サイト用に新しい順 (start_time 降順) で書き出す。
    archives.sort(key=_start_key, reverse=True)
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(archives, f, ensure_ascii=False, indent=2)
    print(f"📁 {ARCHIVE_FILE} 更新完了 ({len(archives)}件)")


def cleanup_old_comments():
    """30日より古いコメントJSONを削除 (GitHub フォルダのみ)。索引エントリ (メタ情報) は
    時系列サイト用に残す。Kick 版と同じ発想。"""
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


# === 未処理の配信を選ぶ ===
def select_pending_streams(archives, cap_per_channel):
    """type=='stream' かつ number_of_comments 未設定のエントリを、
    チャンネルごとに新しい順で最大 cap_per_channel 件ずつ選ぶ。

    「number_of_comments が存在する」を処理済みの目印にしている
    (0件でもチャット無し/アクセス不可として処理済みにする。毎回リトライして
    枠を専有するのを防ぐ)。channel は youtube_meta_fetcher.py が付与する。
    """
    by_channel = {}
    for a in archives:
        if a.get("type") != "stream":
            continue
        if "number_of_comments" in a:
            continue
        by_channel.setdefault(a.get("channel"), []).append(a)

    selected = []
    for items in by_channel.values():
        items.sort(key=_start_key, reverse=True)
        selected.extend(items[:cap_per_channel])
    return selected


# === メイン ===
def main():
    try:
        print("YouTube チャット取得を開始 (ローカル)...")
        local_archives = load_local_archives()
        pending = select_pending_streams(local_archives, MAX_NEW_STREAMS_PER_CHANNEL)
        print(f"未処理の配信: {len(pending)} 件")

        for entry in pending:
            video_id = entry["video_id"]
            print(f"  取得中: {entry.get('title', '')[:40]} ({video_id})")
            comments = get_chat_comments(video_id)
            entry["number_of_comments"] = len(comments)
            save_comment_stats(entry, comments)
            update_archive_data(local_archives)  # 各件ごとに索引保存 (途中終了に強く)
            time.sleep(2)

        if not pending:
            print("チャット未取得の配信はありません。")

        # 30日より古い「コメントファイル」は削除して容量を抑える。
        # 索引エントリ (メタ情報) は時系列サイト用に残す。
        cleanup_old_comments()

        print("✨ 完了")

    except Exception as e:
        print(f"実行中エラー: {e}")


if __name__ == "__main__":
    main()
