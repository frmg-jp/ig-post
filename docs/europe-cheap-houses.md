# 欧州の安い古家（ポルトガル / イタリア / フランス / スペイン）

Cheap Houses 系の海外アカウント（`cheaphouseseu` `cheaphousesportugal`
`cheaphousesinspain` `cheap.houses.france` `cheaphousesinfrance`）を見て、
同じ4か国の物件を扱いたい、という話から起こしたメモ。2026-08-22。

## 1. Instagram からは取らない

**あのアカウントを自動で読んで物件にする経路は作らない。** 理由は2つで、
どちらも単独で決定的。

**規約。** Meta のプラットフォーム規約は、許可なく自動でデータを集める
ことを禁じている。Zillow / Redfin / Compass を「手動URL投入のみ」に
している判断と同じ扱いで、bot 検知の回避もしない。

**写真の権利。** あれらのアカウント自身が、掲載サイト（idealista、
immobiliare、地元の仲介）の写真を転載している。**転載元も権利者では
ない。** そこからさらに @frmg.jpn へ持ってくると、誰の許諾も無いまま
出すことになる。うちが記事から画像を取るときは、引用元と撮影者を本文に
明記できる形にしてある。ここが担保できない。

公式に読める道が1つだけある。**Business Discovery API**（Instagram API
with Facebook Login）は、他の公開プロアカウントのフォロワー数・投稿数・
投稿（キャプション、メディアURL、いいね数など）を返す。ただし、

  - **いまのトークンでは使えない。** @frmg.jpn は Instagram Login で
    発行しており、Business Discovery は Facebook Login 版が要る
  - 返るのは分析用の値。**画像を再掲してよいという話にはならない**

つまり「どんな物件・どんな文面が伸びているか」を調べる用途には使えるが、
物件の仕入れ口にはならない。

## 2. 仕入れは掲載サイトから。idealista の公式APIを使う

あのアカウントが見ているのは掲載サイトそのもの。ならば元から取る方が、
権利の面でも鮮度の面でも素直。

**idealista には公式の Search API がある**（developers.idealista.com）。
申請制で、用途を書いて連絡すると鍵が出る。**スペイン・ポルトガル・
イタリアの3か国を1本でカバーする**ので、4か国のうち3つがこれで済む。

フランスは公式APIが無い。idealista の運用が立ち上がってから、別途
検討する（下の「robots.txt の実測」を参照）。

### 申請文（そのまま貼れる形）

宛先: https://developers.idealista.com/access-request のフォーム。
Name / Email / Project description の3項目。

> **Project description**
>
> We run FREMING CURATED (@frmg.jpn), a Japanese-language Instagram
> publication that introduces architecturally notable homes for sale to
> readers in Japan. We publish one property per day, each with a short
> editorial write-up, and we always credit the source and link readers to
> the original listing.
>
> We would like to use the idealista Search API to find character
> properties in Spain, Portugal and Italy — period houses, converted
> buildings, and homes with identifiable architectural style — and
> present a small, hand-reviewed selection to a Japanese audience.
> Every property is reviewed by a person before anything is published.
>
> Expected usage is low: a few hundred search requests per month, run
> from a scheduled job, not a live site. We do not resell data and we do
> not build a competing listings site. We link back to the idealista
> listing page for each property we feature.
>
> If image usage requires a separate agreement, please let us know the
> right process — we would rather ask first.

日本語（内容の控え）:

> 日本語圏向けに「建築的に見どころのある売出物件」を1日1件紹介している
> Instagram（@frmg.jpn）です。出典を明記し、掲載ページへ誘導しています。
> スペイン・ポルトガル・イタリアの物件を探すために Search API を使いたい。
> 掲載は人が1件ずつ確認してから。リクエストは月に数百件程度、定期実行から。
> データの再販や競合サイトの構築はしません。**写真の利用に別途の許諾が
> 要るなら、その手順を教えてほしい。**

最後の1文は落とさないこと。**画像を出す許諾を先に確認する**のが、この
プロジェクトの立て方そのもの。

### 鍵が出たあとに作るもの

`listing_sources` に `idealista` を足し、経路B（物件ページを直接収集）の
形で実装する。

  - 認証は OAuth2 client credentials。鍵は `.env`（`IDEALISTA_API_KEY` /
    `IDEALISTA_API_SECRET`）に置き、DBにも config にも書かない
  - 1件＝1物件ページなので `source_url` はそのまま `listing_url` になる
    （`collect/relink.py` の「経路Bは写すだけ」の分岐がそのまま効く）
  - **無料枠は小さい。** 月あたりの上限を config に持たせ、超えたら
    その月は止める。上限を「なんとなく」使い切らせない

## 3. robots.txt の実測（2026-08-22）

フランスと、idealista 以外の選択肢を見るために実際に引いた。

| サイト | 国 | robots.txt | 判断 |
|---|---|---|---|
| idealista.com / .it | ES / IT | 一覧・詳細は許可。ajax と並べ替えパラメータのみ拒否 | **APIを使う**（規約が明確） |
| idealista.pt | PT | DataDome のbot判定に当たり robots.txt すら返らない | 取りに行かない。**回避しない** |
| immobiliare.it | IT | 詳細ページは許可。検索マップ・旧PHPは拒否 | 可能性はある。規約の確認が別途要る |
| bienici.com | FR | 一覧・詳細は許可。sitemap あり | **フランスの第一候補** |
| casa.sapo.pt | PT | 大半許可。API/AJAX のみ拒否 | ポルトガルの控え |
| kyero.com | ES/PT | Crawl-delay 1。許可の指定が細かい | 英語圏向け。海外購入者向けで写真が揃う |
| green-acres.fr | FR | `User-agent: *` は許可（Crawl-delay 1）。**ClaudeBot は名指しで拒否** | AI系botを明示的に断っている。**近寄らない** |
| properstar.com | 各国 | Azure WAF のJS判定 | 取りに行かない |

robots.txt が許していても、**利用規約が別に自動収集を禁じていることが
ある**（Zillow がまさにそれ）。ここに「可」と書いたサイトも、実装の前に
規約を1つずつ読む。green-acres のように名指しでAI系を断っているところは、
`User-agent: *` が許していても対象にしない。

## 4. 「安い古家」は別の連載企画にする

いまの承認基準（`docs/approval-criteria.md`）は、**前歴が目に見える形で
残っていること**を最重視している。承認済み8件のうち4件が adaptive_reuse。

Cheap Houses 系が扱うのは別の軸で、**安さと、手を入れる余地**が主役。
石造の廃屋、放棄された村の家、修復前の農家。写真は少なく、様式も
特定できないことが多い。**いまの基準に通すと、ほぼ全部が非承認になる。**
基準を緩めると、既存の投稿の質が一緒に落ちる。

そこで、既存の枠とは別の連載として立てる。

**企画キー**: `cheap_old_house` / 表示名は運用で決める

**この企画だけの見方**（既存の軸を置き換えるもの）:

  - 価格。国の相場に対して明らかに安いこと（絶対額ではなく相対で見る。
    ポルトガルの5万ユーロとフランスの5万ユーロは意味が違う）
  - 手を入れる余地。**Sampling × Renovation の事業そのもの**なので、
    「直せば良くなる」が見えることが価値になる。いまの基準では
    「全面改装済みで物語性なし」が非承認理由になるが、この企画は逆
  - 古さ・素材。石積み、木の小屋組、瓦。**様式が特定できなくても
    落とさない**（いまの基準ではここで落ちる）
  - 写真は少なくてよい。ただしカルーセルを組める最低枚数は要る

**変えないもの**: 売出中であること、出典の明記、画像の権利。

**実装の順**（鍵が出てから着手する。先に作らない）:

  1. `config.yaml` の `series` に `cheap_old_house` を戻す
     （いまは `series: []`。審査UIの企画セレクタが復活する）
  2. スコアリングを企画別に分ける。いまは1本のプロンプトで全部を
     採点している。**この企画の物件を既存のプロンプトに通してはいけない**
     （前歴が無いという理由で0点になる）
  3. 投稿の型を分ける。仕様欄の項目立てが違う（設計者より、価格と
     面積と「何が要るか」が主役）

**先に作らない理由**: 鍵が出るまで実物が1件も無い。どんなデータが
返るか（写真の枚数、説明文の長さ、様式の記載）を見ないまま基準を
書くと、書き直しになる。まず10件見てから決める。
