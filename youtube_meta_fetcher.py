"""
YouTube 動画/配信の「メタ情報」を収集するスクリプト。

新規動画IDの列挙は yt-dlp の flat 抽出 (/streams・/videos タブ一覧) で行い、
詳細情報 (タイトル・開始時刻・長さ・配信/動画の判別) は YouTube Data API v3 で取得する。

なぜ列挙だけ yt-dlp なのか: 実測の結果、配信終了後に "アップロード済み" プレイリスト
(Data API の playlistItems.list が見る場所) へ反映されるまでには数十分〜数時間のラグが
あり、/streams タブの方が先に更新されることを確認した (2026-08-19)。一方、以前 bot 判定
を受けたのは *1件ずつの詳細取得* (yt_dlp.extract_info によるフル innertube 呼び出し) で
あり、flat 抽出 (タブの一覧取得のみ、軽量) は別の処理系なのでリスクは大きく異なる。
詳細取得は今までどおり Data API に任せる (bot 判定の懸念が無い公式API)。

flat 抽出が失敗/ブロックされた場合に備え、Data API の
channels.list→playlistItems.list によるアップロード済みプレイリスト列挙を
フォールバックとして残してある (yt-dlp で新規候補が0件のときのみ追加で確認)。

チャットリプレイ取得は youtube_archiver_with_comments_github.py (ローカル専用) が
別途行う。number_of_comments は付与しない (= チャット取得のローカルジョブが
「未処理」を判定する目印にする)。
"""

import json
import os
import re
import sys
import functools
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from yt_dlp import YoutubeDL

print = functools.partial(print, file=sys.stderr, flush=True)

API_KEY = os.getenv("YOUTUBE_API_KEY")
API_BASE = "https://www.googleapis.com/youtube/v3/"

# youtube_archiver_with_comments_github.py と同じ値に揃えておく (収集範囲を一致させるため)
USER_START_DATE = "2026-08-01T00:00:00+09:00"
CHANNELS = [
    {"handle": "mokouliszt",  "channel_id": "UCZFxcWJS1_iVIFETARRRHZQ"},
    {"handle": "mokoustream", "channel_id": "UCENoC6MLc4pL-vehJyzSWmg"},
]

ARCHIVE_FILE = "youtube_archives.json"
# 1チャンネルあたりのページング上限 (50件/ページ)。安全弁であり、通常は
# 「既知IDのみのページに到達」した時点でもっと早く止まる。
PLAYLIST_PAGE_LIMIT = 10
# yt-dlp flat 抽出で1タブあたり拾う最大件数 (新しい順)
SCAN_LIMIT = 80


# === 新規動画IDの列挙 (yt-dlp flat 抽出、/streams・/videos タブ) ===
def list_channel_video_ids_yt_dlp(channel_id):
    """/streams と /videos タブを新しい順に flat 列挙し、videoId のリストを返す。

    extract_flat のみを使う軽量な一覧取得 (1件ずつのフル extract_info とは異なる)。
    タブ単位で例外を握りつぶし、一方が失敗しても他方は取得を試みる。
    """
    ids = []
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "ignoreerrors": True,
        "extract_flat": "in_playlist",
        "playlistend": SCAN_LIMIT,
    }
    for tab in ("streams", "videos"):
        url = f"https://www.youtube.com/channel/{channel_id}/{tab}"
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = (info or {}).get("entries") or []
            for e in entries:
                if e and e.get("id"):
                    ids.append(e["id"])
        except Exception as e:
            print(f"  yt-dlp列挙エラー [{tab}]: {e}")
    return ids


# === YouTube Data API 呼び出し ===
def api_get(endpoint, params):
    q = dict(params)
    q["key"] = API_KEY
    url = API_BASE + endpoint + "?" + urlencode(q)
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=20) as res:
            return json.loads(res.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"APIエラー [{endpoint}]: HTTP {e.code} {body[:300]}")
        raise
    except URLError as e:
        print(f"APIエラー [{endpoint}]: {e.reason}")
        raise


def fetch_uploads_playlist_id(channel_id):
    data = api_get("channels", {"part": "contentDetails", "id": channel_id})
    items = data.get("items") or []
    if not items:
        return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def list_new_video_ids(uploads_playlist_id, known_ids):
    """アップロード済みプレイリストを新しい順にページングし、未知の video_id を集める。
    1ページ丸ごと既知IDだった場合(=追いついた)、またはページ上限で打ち切る。"""
    ids = []
    page_token = None
    for _ in range(PLAYLIST_PAGE_LIMIT):
        params = {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        data = api_get("playlistItems", params)
        items = data.get("items") or []
        if not items:
            break
        page_new = [
            it["contentDetails"]["videoId"] for it in items
            if it.get("contentDetails", {}).get("videoId") not in known_ids
        ]
        ids.extend(page_new)
        if not page_new:
            break  # このページは全て既知 → 追いついた
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_video_details(video_ids):
    if not video_ids:
        return []
    data = api_get("videos", {
        "part": "snippet,contentDetails,liveStreamingDetails",
        "id": ",".join(video_ids),
    })
    return data.get("items") or []


# === ユーティリティ ===
_DURATION_RE = re.compile(
    r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
)


def parse_iso8601_duration(s):
    """YouTube の ISO8601 duration ("PT1H2M3S" 等) を秒に変換。"""
    m = _DURATION_RE.match(s or "")
    if not m:
        return 0
    d = int(m.group("days") or 0)
    h = int(m.group("hours") or 0)
    mi = int(m.group("minutes") or 0)
    se = int(m.group("seconds") or 0)
    return d * 86400 + h * 3600 + mi * 60 + se


def format_duration(seconds):
    import time
    try:
        s = int(seconds)
        return time.strftime("%H:%M:%S", time.gmtime(s))
    except Exception:
        return "00:00:00"


def parse_iso8601_datetime(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def classify_entry(v, ch, user_start_dt):
    """videos.list の1件を索引エントリに変換。対象外なら None を返す。"""
    vid = v.get("id")
    snippet = v.get("snippet") or {}
    content = v.get("contentDetails") or {}
    live = v.get("liveStreamingDetails")

    if live:
        if not live.get("actualEndTime"):
            # まだ配信中/開始前。アーカイブ化されてから次回以降に拾う。
            return None
        start_iso = live.get("actualStartTime") or snippet.get("publishedAt")
        entry_type = "stream"
    else:
        # 注意: YouTube Premiere (予約公開) も liveStreamingDetails を持つことがあり、
        # 理論上は稀に stream と誤判定される可能性がある。実害は小さい
        # (ローカルのチャット取得ジョブが対象にするだけで、チャットが無ければ
        # number_of_comments=0 として処理済み扱いになるだけ)。
        start_iso = snippet.get("publishedAt")
        entry_type = "video"

    start_dt = parse_iso8601_datetime(start_iso)
    if not start_dt or start_dt < user_start_dt:
        return None

    duration_sec = parse_iso8601_duration(content.get("duration"))
    return {
        "video_id": vid,
        "title": snippet.get("title") or "",
        "start_time": start_dt.isoformat(),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "duration": duration_sec,
        "video_length": format_duration(duration_sec),
        "type": entry_type,
        "channel": ch["handle"],
    }


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# === ローカル保存管理 (youtube_archiver_with_comments_github.py と同じ形式) ===
def load_local_archives():
    if os.path.exists(ARCHIVE_FILE):
        try:
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _start_key(a):
    try:
        return datetime.fromisoformat(a.get("start_time"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def update_archive_data(archives):
    archives.sort(key=_start_key, reverse=True)
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(archives, f, ensure_ascii=False, indent=2)
    print(f"📁 {ARCHIVE_FILE} 更新完了 ({len(archives)}件)")


# === メイン ===
def main():
    if not API_KEY:
        print("エラー: 環境変数 YOUTUBE_API_KEY が未設定です。")
        sys.exit(1)

    print("YouTube メタ情報収集を開始 (Data API v3)...")
    local_archives = load_local_archives()
    known_ids = {a["video_id"] for a in local_archives}
    user_start_dt = datetime.fromisoformat(USER_START_DATE).astimezone(timezone.utc)

    total_new = 0
    for ch in CHANNELS:
        print(f"--- チャンネル: {ch['handle']} ---")
        try:
            # 1) yt-dlp flat 抽出 (最新の /streams・/videos タブを直接見るため反映が早い)
            candidate_ids = list_channel_video_ids_yt_dlp(ch["channel_id"])
            seen = set()
            new_ids = []
            for vid in candidate_ids:
                if vid in known_ids or vid in seen:
                    continue
                seen.add(vid)
                new_ids.append(vid)
            print(f"  yt-dlp列挙: 新規候補 {len(new_ids)} 件")

            # 2) フォールバック: yt-dlpで新規が見つからなかった場合のみ、念のため
            #    Data API のアップロード済みプレイリストでも確認する (安価: 数unit)。
            if not new_ids:
                uploads_id = fetch_uploads_playlist_id(ch["channel_id"])
                if uploads_id:
                    new_ids = list_new_video_ids(uploads_id, known_ids)
                    if new_ids:
                        print(f"  フォールバック(Data API)で新規候補 {len(new_ids)} 件")

            for batch in chunks(new_ids, 50):
                details = fetch_video_details(batch)
                for v in details:
                    entry = classify_entry(v, ch, user_start_dt)
                    if entry is None:
                        continue
                    local_archives.append(entry)
                    known_ids.add(entry["video_id"])
                    total_new += 1
                    print(f"  新規[{entry['type']}]: {entry['title'][:40]} ({entry['video_id']})")
        except Exception as e:
            print(f"[{ch['handle']}] 収集エラー: {e}")

    update_archive_data(local_archives)

    if total_new == 0:
        print("新しいアーカイブ/動画はありません。")
    print(f"✨ 完了 (新規 {total_new} 件)")


if __name__ == "__main__":
    main()
