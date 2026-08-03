# 仲介サイト26件の実測（2026-08-03）

対象は「全米の物件情報を扱うサイト」一覧のうち、区分 C（仲介・地域
ブローカー）の26件。**ユーザー判断により自動収集の対象にした**うえで、
実際にどこまで取れるかを測った記録。

## 前提（判断の経緯）

これらのサイトの掲載物件には、MLS から IDX で供給された他社物件が
含まれる。IDX 参加規約は再配布・スクレイピング・写真の再利用を禁じており、
自社専任物件と IDX 由来をサイト上で機械的に分離することはできない。
写真の権利も掲載ブローカー側にある。

この点を伝えたうえで「構わないので取得する」という判断があった。
ここに書いてあるのは、その判断のもとで**技術的に何が可能か**の実測。

守るものは変えていない:

- robots.txt の尊重（`http.respect_robots_txt` は false にできない）
- 同一ドメインへ3秒間隔・並列アクセスなし
- User-Agent に連絡先を明記
- **bot検知の回避は実装しない**

## 到達可否

| 状態 | 件数 | サイト |
| --- | --- | --- |
| WAFが403 | 10 | Keller Williams, RE/MAX, The Agency, Rodeo Realty, Corcoran, Brown Harris Stevens, Baird & Warner, Intero, Premiere Property Group, Realogics Sotheby's |
| robots許可・サーバ描画 | 11 | Coldwell Banker, Hilton & Hyland, Beverly Hills Estates, Nest Seekers, Dream Town, @properties, Vanguard, Corcoran Icon, Living Room Realty, Windermere, CB Bain |
| robots許可・JS描画 | 5 | Sotheby's, Christie's, Century 21, ERA, John L. Scott |

403 の10件は robots.txt すら返さない。取得するには検知回避が要るので
対象外にしている。JS描画の5件は HTML に物件データが載っておらず、
収集経路に Playwright を組み込まない限り取れない（未着手）。

robots.txt が物件ページを Disallow していたサイトは無かった。
Century 21 と ERA が `/search` を、Hilton & Hyland が `Crawl-delay: 5` を
宣言している。Crawl-delay は `HttpClient` が自動で従う。

## サイトマップ

多くが物件ページを sitemap に載せている。差分収集の入口として RSS より
素直に使える。

| サイト | サイトマップ | 備考 |
| --- | --- | --- |
| Coldwell Banker | `sitemapindex-listings-new-day.xml` | 当日追加分だけの一覧がある |
| @properties | `newestlistings_index.xml` | 同上 |
| Christie's | `sitemap-homes-for-sale-index/` | JS描画のため未着手 |
| Vanguard | `sitemap.xml` → `sitemap-properties-dpages--0.xml` | 物件 10,833件 |
| Dream Town | `sitemap.xml` に物件ページが無い | 一覧ページから拾う |

## 実装済み（2026-08-03）

`src/freming/collect/listings.py`（経路A）。sitemap または一覧ページを
入口に、物件URLを絞ってから詳細ページを取る。抽出規則は
`config.yaml` の `listing_sources[].crawl` に持たせてあり、サイトを
足すのにコードは要らない。

通したのは実測で確認できた2件:

- **dreamtown**（Chicago）— 一覧ページ起点。og:title が住所
- **vanguard**（San Francisco）— sitemap 起点。住所は本文にしかない

どちらも `enabled: false` で登録してある。使うときに true にする。

### 詰まったところ

- **価格と住所は `<p>` の地の文に無い。** `parse_page` は関連記事の混入を
  防ぐため `<p>` だけを本文にするが、物件ページでは価格も住所も見出しや
  専用のボックスに入る。地の文で見つからないときはページ全体から拾い直す
- **物件URLの絞り込みを緩めると一覧ページを取り違える。** Vanguard で
  `/properties/[^/]+$` としたところ `/properties/commercial` を物件として
  取得し、ページ内の最高額 $15,500,000 を物件価格として登録しかけた。
  物件ページは末尾にMLS番号が付くので、そこまで含めて絞る
- **URLの slug から市名は取れない。** `95-highland-way-inverness-ca-94937`
  の街路と市名の境は書式から判別できず、「Way Inverness」になった。
  表示されている住所を読む方が確実
- **見出しを持たない物件ページがある。** Vanguard は `<title>` も
  `og:title` も空で、住所は本文にしかない。URLをタイトルに据えると
  審査UIで何の物件か分からないので、住所を代わりに使っている

## 残っていること

- 残り9件（サーバ描画で未着手）のうち、Coldwell Banker と @properties は
  当日追加分の sitemap があるので費用対効果が高い
- JS描画の5件を対象にするなら Playwright の組み込み（1〜2日規模）
- **拾える物件が編集方針と合うかは未検証。** Dream Town で取れた8件は
  一般的なコンドミニアムと戸建てで、承認基準（前歴が残る一点物）とは
  重ならなかった。スコアリングで落ちる想定だが、その分だけ採点の
  API 呼び出しが増える。件数を絞る仕組みが要るかもしれない
