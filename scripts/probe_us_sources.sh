#!/usr/bin/env bash
# =====================================================================
# 全米を扱うソースの候補をまとめて検証する。
#
# 候補リスト: docs/us-source-candidates.tsv（区分の意味もそこに書いてある）
#
# 3段階に分けてある。1が一番当たりの見込みが高いので、時間が無ければ
# 1だけでもよい。
#
#   1. config.yaml に登録済みなのに feeds が空のまま（単純な調査漏れ）
#   2. 売出物件を扱う編集メディア
#   3. 地域メディア・保存系
#
# 注意 —— probe-feed は必ず fetch_article_pages=False で判定する。
# 抜粋しか配信しないフィード（The Spaces がそうだった）は、中身が良くても
# 候補0件と出る。「審査 要再測定（抜粋配信）」が出たものは、config.yaml に
# fetch_article_pages: true で登録してから collect --explain で判断する。
#
# robots.txt を尊重し、リクエスト間隔3秒・同一ドメイン並列なしは
# HttpClient が強制する。DBには書き込まない。所要は10分ほど。
#
# 使い方:
#   bash scripts/probe_us_sources.sh 2>&1 | tee /tmp/probe_us.log
# =====================================================================
set -uo pipefail

FREMING="python -m freming.cli"

echo "###############################################################"
echo "# 1. 登録済みなのにフィード未調査のソース"
echo "#    Dwell は rank S でアメリカ中心。ここが空だったのは単純な漏れ。"
echo "###############################################################"
$FREMING discover-feed --probe \
  https://www.dwell.com/ \
  https://www.archdaily.com/ \
  https://www.architecturalrecord.com/ \
  https://www.architecturaldigest.com/ \
  https://www.iconichouses.org/

echo
echo "###############################################################"
echo "# 2. 売出物件を扱う編集メディア"
echo "#    Robb Report は本文が薄い（166〜199字）。The Spaces と同じ形なので、"
echo "#    ここで候補0件でも切らないこと。"
echo "###############################################################"
$FREMING discover-feed --probe \
  https://www.curbed.com/ \
  https://www.atomic-ranch.com/ \
  https://usmodernist.org/ \
  https://midcenturyhome.com/ \
  https://www.dirt.com/

$FREMING probe-feed https://robbreport.com/shelter/feed/

echo
echo "###############################################################"
echo "# 3. 地域メディア・保存系"
echo "#    「前歴が目に見える形で残っている」という承認基準（docs/"
echo "#    approval-criteria.md）と、保存系メディアは相性が良いはず。"
echo "###############################################################"
$FREMING discover-feed --probe \
  https://socketsite.com/ \
  https://la.urbanize.city/ \
  https://therealdeal.com/ \
  https://savingplaces.org/ \
  https://www.oldhouseonline.com/ \
  https://docomomo-us.org/

echo
echo "###############################################################"
echo "# 4. 掲載写真を再掲する型（権利の確認が要る）"
echo "#    フィードが読めても、そのまま有効化しないこと。写真はMLS・"
echo "#    ブローカー・売主に帰属する。portlandscondos と同じ扱いにする。"
echo "###############################################################"
$FREMING discover-feed --probe \
  https://www.oldhousedreams.com/ \
  https://www.cheapoldhouses.com/

echo
echo "###############################################################"
echo "# 終わり"
echo "###############################################################"
cat <<'MSG'
結果の貼り方の目安:

  「審査 週N件」が付いたもの        → そのまま登録できる
  「審査 要再測定（抜粋配信）」      → fetch_article_pages: true が要る
  「候補0件」で本文が厚いもの        → 本当に売出記事が無い。dezeen と同じ扱い
  NG                                → 理由（robots / 404 / フィード無し）ごとに対応が変わる

区分が relist のもの（Old House Dreams / Cheap Old Houses）は、
数字が良くても enabled: false で登録します。掲載写真の権利が別問題のため。
MSG
