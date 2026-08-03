#!/usr/bin/env bash
# =====================================================================
# 未調査のまま残っている4サイトを試す。
#
#   thespaces.com        … 一般（建築 × 不動産）
#   themodernspaces.com  … 一般（モダン住宅）
#   metalocus.es         … スペイン ← 重点エリアで未充足
#   portlandscondos.com  … Portland ← 重点エリアで未充足
#
# 2段階でやる:
#   1. discover-feed --probe … サイトが宣言しているフィードを探して試す。
#      URLを推測すると外す（Wallpaper* が /architecture/feed ではなく
#      /feeds.xml だった例がある）。まずこちらを信じる。
#   2. probe-feed          … 1で何も見つからなかった場合に、推測URLを直接試す。
#
# DBには一切書き込みません（probe は dry-run）。
# robots.txt を尊重し、リクエスト間隔3秒・同一ドメイン並列なしは
# HttpClient が強制します。所要は5分ほど。
#
# 使い方:
#   bash scripts/probe_remaining_feeds.sh 2>&1 | tee /tmp/probe_remaining.log
#
# 見るところ:
#   件数 … フィードが配信している記事数
#   本文中央値 … 1000字を切ると判定材料として薄い（採用済みは1555〜5088字）
#   候補 … 販売シグナルを満たした件数。ここが0でも、編集メディアなら
#          「販売記事は時々」なので即NGにはしない（dezeen が0件だった例がある）
# =====================================================================
set -uo pipefail

FREMING="python -m freming.cli"

echo "###############################################################"
echo "# 1. サイトが宣言しているフィードを探して試す"
echo "###############################################################"
$FREMING discover-feed --probe --details \
  https://thespaces.com/ \
  https://themodernspaces.com/ \
  https://www.metalocus.es/ \
  https://portlandscondos.com/

echo
echo "###############################################################"
echo "# 2. 推測していたフィードURLを直接試す（1で見つからなかった場合の保険）"
echo "###############################################################"
$FREMING probe-feed --details \
  https://thespaces.com/feed/ \
  https://themodernspaces.com/feed/ \
  https://www.metalocus.es/en/rss.xml \
  https://portlandscondos.com/feed/

echo
echo "###############################################################"
echo "# 終わり"
echo "###############################################################"
echo "OK が出たフィードURLを貼ってください。config.yaml への登録と、"
echo "assume_for_sale / url_exclude / fetch_article_pages の判断をします。"
