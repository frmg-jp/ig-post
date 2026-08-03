#!/usr/bin/env bash
# =====================================================================
# 承認済みの納品を Mac に自動実行させる（launchd）。
#
# 審査は Render に置いた審査UIで行い、承認したものをこの Mac が
# 15分おきに拾って Drive へ納品する。登録は1回だけ。以後ターミナルは
# 開かなくてよい。
#
# 使い方:
#   bash scripts/install-delivery-agent.sh
#
# 外すとき:
#   bash scripts/install-delivery-agent.sh --uninstall
#
# 動くのは Mac が起きている間だけ。スリープ中は止まり、開くと再開する。
# 納品は最大15分遅れる。
# =====================================================================
set -uo pipefail

LABEL="jp.frmg.freming.deliver"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INTERVAL=900

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  rm -f "$PLIST"
  echo "自動納品の登録を外しました。"
  exit 0
fi

PYTHON="$REPO/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "仮想環境が見つかりません: $PYTHON" >&2
  echo "先に python3 -m venv .venv と pip install -e \".[postgres]\" を済ませてください。" >&2
  exit 1
fi

if [ ! -f "$REPO/.env" ]; then
  echo ".env がありません: $REPO/.env" >&2
  echo "DATABASE_URL を書いた .env が要ります（納品先のDBを見るため）。" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"

# StartInterval は前回が動いている間は次を起こさない（launchd が同じ
# ラベルのジョブを二重に起動しない）。納品が15分を超えても重ならない。
cat > "$PLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>freming.cli</string>
    <string>deliver</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO</string>
  <key>StartInterval</key>
  <integer>$INTERVAL</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$REPO/logs/deliver-agent.log</string>
  <key>StandardErrorPath</key>
  <string>$REPO/logs/deliver-agent.log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
PLIST_END

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
if ! launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
  echo "登録に失敗しました。$PLIST を確認してください。" >&2
  exit 1
fi

echo "自動納品を登録しました。"
echo "  間隔    $((INTERVAL / 60)) 分ごと（Mac が起きている間）"
echo "  ログ    $REPO/logs/deliver-agent.log"
echo "  外す    bash scripts/install-delivery-agent.sh --uninstall"
echo
echo "いま1回動かして確かめるには:"
echo "  launchctl kickstart -p gui/$(id -u)/$LABEL"
