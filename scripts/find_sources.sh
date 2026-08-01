#!/usr/bin/env bash
# =====================================================================
# ソース探しを一度に走らせる。
#
#   1. フィードURLが不明なサイト → discover-feed --probe で探して試す
#   2. Chicago / Brooklyn の候補フィード → probe-feed
#   3. CIRCA のURL構造 → probe-feed --details（url_exclude を決めるため）
#
# すべて robots.txt を尊重し、フィード1回ずつのリクエストしか送りません。
# リクエスト間隔3秒・同一ドメイン並列なしは HttpClient が強制します。
# サイト数が多いため 5〜10分ほどかかります。
#
# 使い方:
#   bash scripts/find_sources.sh 2>&1 | tee /tmp/find_sources.log
# =====================================================================
set -uo pipefail

FREMING="python -m freming.cli"

echo "###############################################################"
echo "# 1. フィードURLが不明なサイトを調べる"
echo "###############################################################"
$FREMING discover-feed --probe \
  https://themodernhouse.com/ \
  https://www.dirt.com/ \
  https://www.mansionglobal.com/ \
  https://www.wallpaper.com/ \
  https://socalmodern.com/ \
  https://beyondshelter.com/ \
  https://architectureforsale.com/ \
  https://www.urbnlivn.com/ \
  https://www.plataformaarquitectura.cl/ \
  https://thesingular.space/

echo
echo "###############################################################"
echo "# 2. Chicago / Brooklyn の候補フィードを試す"
echo "###############################################################"
$FREMING probe-feed \
  https://chicagoyimby.com/feed \
  https://www.chicagoarchitecture.org/feed/ \
  https://www.dreamtown.com/blog/feed \
  https://www.brownstoner.com/feed/ \
  https://www.6sqft.com/feed/ \
  https://newyorkyimby.com/feed

echo
echo "###############################################################"
echo "# 3. CIRCA のURL構造を確認する（url_exclude を決めるため）"
echo "###############################################################"
$FREMING probe-feed --details https://circaoldhouses.com/feed/

echo
echo "完了しました。"
echo "  - 「候補N件」が出たフィードを config.yaml の editorial_sources に登録"
echo "  - CIRCA の「URLパターン」を見て、物件ページ以外を url_exclude に指定"
