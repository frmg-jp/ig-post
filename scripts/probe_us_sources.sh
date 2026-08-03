#!/usr/bin/env bash
# =====================================================================
# アメリカの物件を増やすためのソース探し。
#
# 1. Robb Report / Shelter
#    「本文が薄いので保留」として置いてあったが、これは thespaces と
#    まったく同じ状況だった。thespaces は本文中央値240字で candidate 0件
#    だったのが、fetch_article_pages: true にしたら 4/10 件になった。
#    probe-feed は必ず fetch_article_pages=False で判定するので、
#    抜粋配信のフィードは構造的に候補0件になる。ここでは記事ページまで
#    取って判定する（collect --dry-run を使う理由）。
#
# 2. config.yaml に登録済みなのに feeds が空のままのソース
#    dwell / archdaily / arch_record / domus / ad / iconic_houses。
#    特に Dwell は rank S でアメリカ中心。単純な調査漏れ。
#
# robots.txt を尊重し、リクエスト間隔3秒・同一ドメイン並列なしは
# HttpClient が強制します。所要は5〜10分。
#
# 使い方:
#   bash scripts/probe_us_sources.sh 2>&1 | tee /tmp/probe_us.log
# =====================================================================
set -uo pipefail

FREMING="python -m freming.cli"

echo "###############################################################"
echo "# 1. 登録済みなのにフィード未調査のソースを探す"
echo "###############################################################"
$FREMING discover-feed --probe --details \
  https://www.dwell.com/ \
  https://www.archdaily.com/ \
  https://www.architecturalrecord.com/ \
  https://www.domusweb.it/ \
  https://www.architecturaldigest.com/ \
  https://www.iconichouses.org/

echo
echo "###############################################################"
echo "# 2. Robb Report / Shelter（保留中だったもの）"
echo "###############################################################"
echo "※ 本文が166〜199字と薄いので、ここでの候補0件は想定内です。"
echo "   判断は下の collect --dry-run の方で行います。"
$FREMING probe-feed --details \
  https://robbreport.com/shelter/feed/

echo
echo "###############################################################"
echo "# 終わり"
echo "###############################################################"
echo "OK が出たフィードURLを貼ってください。"
echo "Robb Report を試すなら、config.yaml に fetch_article_pages: true で"
echo "登録したうえで、記事ページまで取って判定します:"
echo "  python -m freming.cli collect --source robbreport --limit 10 --dry-run"
