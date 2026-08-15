# run_and_push.ps1
# ローカルでチャットを収集し、結果を GitHub へ push する (Windows タスクスケジューラ用)。
# 家庭の IP から実行するため bot 判定を回避しやすく、cookies.txt を置けばメンバー限定も取得可能。
# push した内容を GitHub Pages が配信し、ブラウザ拡張がそれを読む。

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$log = Join-Path $PSScriptRoot "run_local_log.txt"
function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

Log "=== ローカル収集開始 ==="

# 公開用フォルダ (comments_github) に出力させる
$env:YT_PUBLISH = "1"

# cookies.txt があればメンバー資格付きで取得 (メンバー限定も対象になる)
$cookie = Join-Path $PSScriptRoot "cookies.txt"
if (Test-Path $cookie) {
    $env:YT_COOKIES_FILE = "cookies.txt"
    Log "cookies.txt を使用 (メンバー限定も取得対象)"
} else {
    Log "cookies.txt なし → 公開配信のみ・cookie 無しで実行"
}

# yt-dlp を最新化 (YouTube 側変更への追随に重要)
python -m pip install --quiet --upgrade yt-dlp
Log "yt-dlp 更新確認 (exit=$LASTEXITCODE)"

# 収集本体を実行
python youtube_archiver_with_comments_github.py
Log "収集スクリプト終了 (exit=$LASTEXITCODE)"

# 変更を commit & push (cookies.txt / comments_local / ログは .gitignore 済みで対象外)
git add -A
git commit -m "Update archives and comments (local)"
if ($LASTEXITCODE -eq 0) {
    git push
    if ($LASTEXITCODE -eq 0) { Log "push 成功" } else { Log "push 失敗 (要確認)" }
} else {
    Log "変更なし → commit/push スキップ"
}

Log "=== 完了 ==="
