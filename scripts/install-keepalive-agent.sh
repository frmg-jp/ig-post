#!/usr/bin/env bash
# =====================================================================
# Render に置いた審査UIを温めておく（launchd）。
#
# Render の無料プランは15分アクセスが無いとコンテナを落とす。次に開いた
# 人が起動を待たされる。実測でコールドスタート 33秒、ウォーム 0.7秒。
# アプリ自体の起動は 1.2秒なので、待ち時間はほぼ Render 側のコンテナ
# 起動で、コードでは縮められない。開く前に叩いて起こしておくしかない。
#
# 叩くのは /healthz だけ。この経路は認証を通さず、DBにも触らない
# （web/app.py の healthz は固定の辞書を返すだけ）。つまり:
#   - 資格情報を plist に置かなくてよい
#   - Neon を起こさないので 100 CU-hours/月 の枠を減らさない
#
# 使い方:
#   bash scripts/install-keepalive-agent.sh
#   bash scripts/install-keepalive-agent.sh https://別のURL
#
# 外すとき:
#   bash scripts/install-keepalive-agent.sh --uninstall
#
# 動くのは Mac が起きている間だけ。寝ている間は叩かないので、Render の
# 無料枠（750 instance-hours/月）も使い切らない。朝いちばんの1回目だけは
# 従来どおり待つことになる。
# =====================================================================
set -uo pipefail

LABEL="jp.frmg.freming.keepalive"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Render が落とすのは15分。10分ならスリープ復帰の揺れがあっても間に合う。
INTERVAL=600
DEFAULT_URL="https://freming-curated-review.onrender.com"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  rm -f "$PLIST"
  echo "審査UIの温め直しを外しました。"
  exit 0
fi

BASE_URL="${1:-$DEFAULT_URL}"
BASE_URL="${BASE_URL%/}"
case "$BASE_URL" in
  https://*) ;;
  *)
    echo "URLは https:// で始めてください: $BASE_URL" >&2
    exit 1
    ;;
esac
HEALTH_URL="$BASE_URL/healthz"

echo "疎通を確かめます: $HEALTH_URL"
# コールドスタートを踏むと1分近くかかることがあるので待つ。
code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 180 "$HEALTH_URL" || echo "000")
if [ "$code" != "200" ]; then
  echo "  /healthz が 200 を返しません（status=$code）。URLを確認してください。" >&2
  exit 1
fi
echo "  OK"

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"

# --max-time を付けるのは、応答しないときに次回まで居座らせないため。
# 失敗しても何もしない（|| true）。温めそこねても実害は待ち時間だけで、
# ここでエラーを出しても直せることが無い。
cat > "$PLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>curl -sS -o /dev/null --max-time 120 "$HEALTH_URL" || true</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO</string>
  <key>StartInterval</key>
  <integer>$INTERVAL</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$REPO/logs/keepalive-agent.log</string>
  <key>StandardErrorPath</key>
  <string>$REPO/logs/keepalive-agent.log</string>
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

echo "審査UIの温め直しを登録しました。"
echo "  対象    $HEALTH_URL"
echo "  間隔    $((INTERVAL / 60)) 分ごと（Mac が起きている間）"
echo "  ログ    $REPO/logs/keepalive-agent.log"
echo "  外す    bash scripts/install-keepalive-agent.sh --uninstall"
