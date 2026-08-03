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

通したのは実測で確認できた4件:

- **dreamtown**（Chicago）— 一覧ページ起点。og:title が住所
- **vanguard**（San Francisco）— sitemap 起点。住所は本文にしかない
- **coldwellbanker**（全米）— 当日追加分の sitemap。物件 6,372件
- **corcoranicon**（San Francisco）— sitemap。物件 5,015件

いずれも `enabled: false` で登録してある。使うときに true にする。

### 通らなかったもの（サーバ描画だが収集できない5件）

| サイト | 理由 |
| --- | --- |
| @properties | sitemap を置く `resources.atproperties.com` の robots.txt が 403。取得できない robots.txt は「許可されていない」と扱う（fail-closed）ので収集できない。www 側に sitemap があれば再挑戦できる |
| Nest Seekers | 物件は 19,944件あり取得もできるが、**所在地を誤る**。カナダの物件が混ざっており、国の判定を持たないパイプラインでは United States として登録してしまう |
| Hilton & Hyland | robots が宣言する `sitemap_index.xml` が 404。一覧ページ `/properties/sale/` の物件リンクはJSで描画されており、HTMLに出ない |
| Living Room Realty | sitemap はブログ記事のみ。`/listings` の物件リンクもJSで描画 |
| Windermere | sitemap はブログ記事のみ。トップに物件リンクが1本も無い |
| CB Bain | 物件リンクが `/propertydrawer/` だけ。一覧はJS |

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
- **フッターの自社住所を物件の所在地として拾う。** Nest Seekers は
  カナダ Wasaga Beach の物件に「New York」、Beverly Hills Estates は
  Bel Air の物件に「West Hollywood」が付いた。ヘッダーとフッターを
  タグで落とす手も試したが、Coldwell Banker は価格と住所を `<header>` の
  中に置いており、落とすと物件の情報まで消えた。**URLに現れる市名と
  突き合わせて選ぶ**方式にしてある（会社の住所はURLに出てこない）。
  決められないときは所在地を空にする。誤った所在地を入れるよりよい

## 残っていること

- **26件中4件が収集できる状態**。残り22件の内訳は、WAFの403が10件、
  JS描画が5件（Sotheby's / Christie's / Century 21 / ERA / John L. Scott）、
  サーバ描画だが収集できないものが5件（上表）、Beverly Hills Estates は
  所在地が空になるので保留、Nest Seekers は国の取り違えで保留
- JS描画のサイトを対象にするなら Playwright の組み込み（1〜2日規模）。
  これで Christie's・Century 21・Hilton & Hyland・Living Room・Windermere・
  CB Bain の6件が射程に入る
- 物件ごとの国の判定。いまは `crawl.country` にサイト単位で固定値を
  持たせているだけなので、複数国を扱うサイト（Nest Seekers）を足せない
- **拾える物件が編集方針と合うかは未検証。** Dream Town で取れた8件は
  一般的なコンドミニアムと戸建てで、承認基準（前歴が残る一点物）とは
  重ならなかった。スコアリングで落ちる想定だが、その分だけ採点の
  API 呼び出しが増える。件数を絞る仕組みが要るかもしれない
