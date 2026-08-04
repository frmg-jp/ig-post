# 引き継ぎ（2026-08-03）

前セッション（環境: デフォルト）から、ネットワーク制限のない環境
「ネットオープン」への移行用。**最初にこのファイルを読むこと。**

## 作業ブランチ

`claude/freming-curated-pipeline-mfzomz`（このファイルを含む最新をプッシュ済み）。
開発・プッシュはこのブランチのみ。他ブランチへは押さない。

## 完了: 審査ページのトンマナ合わせ

依頼原文:
「審査ページを以下FREMINGの公式ページのデザインにトンマナを合わせて。
フォントも再調整。 https://frmg.jp/」

前セッションは frmg.jp に到達できず（ゲートウェイ403）、現物を見ずに
「編集メディア風」の仮デザインを実装していた（コミット `cd3ed95`）。
ネットオープン環境で frmg.jp を取得し、実測値に差し替え済み。

読み取った値（出典: `wp_freming/style.css` の `:root` と算出スタイル）:

| 項目 | 値 |
| --- | --- |
| 地 | `#EFEFEF` |
| 文字 | `#1C1B1B` / グレー `#707070` / 罫 `#CFCFCF` |
| アクセント | `#FF3500`（`.hover-red` の一点だけ） |
| 和文 | `hiragino-mincho-pron` 300・16px・行送り 2.0 |
| 欧文 | `ibm-plex-mono` 400・大文字・行送り 1.25 |
| 角丸 | なし（円形ボタンが1箇所だけ） |
| ダークモード | なし |

実装側の対応:

- 和文は端末のヒラギノ明朝 →游明朝→ Noto Serif JP、欧文は Google Fonts の
  IBM Plex Mono。本家の Adobe Fonts キットは frmg.jp ドメイン専用で使えない
- ダークモードのブロックは削除（本家がライトのみ）
- カードの囲みを外し、information 一覧と同じ下罫1本の行組みにした
- **承認＝深緑 `#1f5c3d` / 非承認＝煉瓦 `#97372a` だけは本家のパレット外**。
  取り違えると納品事故になるため、ユーザー判断で色を残している。
  赤 `#FF3500` はスコア・企画タグ・Drive リンク専用

## プロジェクト概要

FREMING CURATED — 建築キュレーションメディアの物件収集パイプライン。

```
[1] 収集 → [2] 判定 → [3] 審査UI → [4] 画像取得 → [5] 正方形加工 → [6] Drive納品
                ↑ 非承認理由を feedback に記録し次回に反映 [7] 学習ループ
```

- Python / FastAPI + Jinja2 / SQLite（Postgres 両対応済み・移行は未実施）
- 審査UIの起動: `python -m freming.cli serve`
- 収集: `python -m freming.cli collect --source <name>`（`--dry-run --explain` が検証用）
- 承認→納品は自動（`DeliveryWorker`。`delivery.auto: true`）

## 遵守事項（ユーザー指定のポリシー。緩めない）

- 公式RSS優先、足りないときのみ sitemap.xml
- robots.txt を必ず確認し Disallow を尊重（実装済み・fail-closed）
- リクエスト間隔は最低3秒。同一ドメインへの並列アクセス禁止
- User-Agent に連絡先を明記
- Zillow / Redfin / Compass は自動収集禁止 → 手動URL投入モードのみ
- ログイン・CAPTCHA回避・レート制限回避は実装しない
- 鍵は `credentials/service-account.json`（gitignore 済み）
- 再実行しても重複納品しない（`source_url` UNIQUE + `deliveries` チェック）

## フィードの窓とバックフィル（2026-08-03）

**公式RSSはたいてい最新10件しか配信しない。** それより古い記事は、
毎日回していても一度も現れないまま落ちる。ここを取りこぼしていた。

The Spaces の実測:

| | 期間 | 物件の件数 |
| --- | --- | --- |
| サイト全体フィード（旧） | 7.2日分 | 6件（4件は非物件に食われる） |
| カテゴリ専用フィード（新） | 12.2日分 | 9件 |
| ＋一覧ページのバックフィル | — | 合計15件 |

対応:

- `feeds` を `https://thespaces.com/category/property/feed/` に変更
- `EditorialSource.index_urls` を追加。フィードの処理のあとに一覧ページを
  見て、まだDBに無い記事を拾う（`editorial.py` の `_backfill`）
- 2回目以降はほぼ全件が「取得済み」で終わる。増えるリクエストは一覧ページ
  1回だけなので毎回回してよい。切るなら `collect --no-backfill`

**混ぜてはいけないものが2つある**（どちらもテストで固定した）:

- **配信ペースの分子。** バックフィルの分を混ぜると `candidates_per_week`
  が跳ね上がる。`feed_inserted` を使うこと
- **`--dry-run` でのフィード分の再取得。** 書き込まないので
  `exists_source_url` が効かず、同じ記事を二度取って件数も二重になる。
  `stats.entry_urls` で除外している

`index_urls` を足すときは **`url_include` も必ず書く**。一覧ページには
ナビゲーションやカテゴリのリンクが混ざる。記事URLと同じ形をしたもの
（`/about` など）は `url_exclude` で落とす。

### 4ソースの実測（2026-08-03）

フィードの窓は「件数」ではなく「何日分か」で見る。`lookback_days: 30` を
下回っていると、定期実行が止まった日数ぶんそのまま取りこぼす。

| ソース | 窓 | 日数 | 一覧ページ | 判断 |
| --- | --- | --- | --- | --- |
| 6sqft | 10件 | **2.9日** | distinctive-homes に186件 | **追加した** |
| Dezeen | 50件 | 6.0日 | — | 403で不可 |
| The Spaces | 10件 | 12.2日 | property に21件 | 追加済み |
| Dwell | 30件 | 23.8日 | real-estate に30件 | **追加した**（過去分のため） |
| WowHaus | 10件 | **45日** | — | 不要（窓が lookback より長い） |

- **6sqft は窓が2.9日しかない。** 4つの中で最も薄く、3日止まると落ちる。
  しかも直近10件は物件記事が0件で、物件は distinctive-homes カテゴリに
  まとまっている。バックフィルの効果が一番大きい
- **WowHaus は不要。** 窓が45日で `lookback_days: 30` を上回っており、
  日々の取りこぼしが構造的に起きない
- **Dezeen と WowHaus は一覧ページを取れない。** データセンターのIPから
  HTMLページが 403 になる（フィードは通る）。GitHub Actions で記事ページが
  403 だったのと同じ現象で、この環境からも再現した。User-Agent の偽装に
  よる回避は行わない

### バックフィルで踏んだ2つの穴（修正済み・テストあり）

- **上限で切ってから取得済みを落とすと永久に進まない。** 6sqft の一覧は
  186件あり、上限20件で切ってから取得済みを弾くと、2回目以降は毎回同じ
  先頭20件を見て0件で終わる。**取得済みを先に落としてから上限で切る**
- **`fetch_article_pages: false` のソースでバックフィルが働かない。**
  この設定は「フィードが全文を配信するので取りに行かなくてよい」の意味で、
  一覧ページ由来の記事には当てはまらない（フィード本文が無い）。
  バックフィルのときだけ記事ページを取る。ただし**サイト側に拒否され
  続けた場合は止める**（`_fetch_off_by_failures`。設定による無効化と
  失敗による無効化を分けてある）

## 審査UIの共有（2026-08-03）

担当者と一緒に審査するため、審査UIを外に出せるようにした。
手順は docs/review-ui-hosting.md（Render・無料枠・Basic認証）。

実装側で入れた歯止め:

- ループバック以外で待ち受けるときは `REVIEW_UI_USER` と
  `REVIEW_UI_PASSWORD` が必須（`web/auth.py` の `require_credentials`）。
  **認証なしで外向けに立ち上がる経路は作らない**
- 公開用の入口は `freming.web.asgi:app`。`serve` と違い、資格情報が
  無ければ起動せず、`delivery.auto` を落とす
- **納品ワーカーは1箇所だけで動かす。** 2箇所で回すと同じ物件を二重に
  納品する（`already_delivered` の確認から Drive 書き込みまでに隙間があり、
  フォルダ名も max+1 なので同じ frmg_igNNN を取り合う）。納品は Mac 側に
  一本化し、同一マシン内の重複は `data/delivery.lock` のファイルロックで
  止めている（`delivery/lock.py`）
- 納品は launchd で15分おきに自動実行する
  （`scripts/install-delivery-agent.sh`）。**常駐にしないのは Neon の
  無料枠のため。** ポーリングし続けるとDBが自動停止せず、月100 CU時間を
  使い切る。Render 側で納品しないのは、記事ページがデータセンターのIPから
  403 になる実測（GitHub Actions で Dezeen / WowHaus が全滅）と、
  Drive の認証が本人アカウントの OAuth だから

## 採点のモデル（2026-08-03 に変更）

費用を抑えるため `claude-sonnet-5` → **`claude-haiku-4-5`** に下げた
（`config.yaml` の `scoring.model`）。判定の精度は落ちる。1〜2週間ぶん
溜まったら、Sonnet 5 のときの点と見比べて戻すかどうか決める。

同時に `scoring.effort` を **null** にしてある。**effort は Opus 4.5 以降と
Sonnet 4.6 以降にしか無いパラメータで、Haiku 4.5 に渡すと 400 になる。**
400 は `_is_retryable` が再試行しないので、その場で全件失敗する。
`client.py` の `_output_config` が null のときに送らない作りにしてある。
上位モデルに戻すときは `effort: medium` も一緒に戻すこと
（`tests/test_scoring.py` に組み合わせの取り違えを止めるテストがある）。

概算の費用（実測の入力量から）:

| モデル | 単価（入力/出力・per MTok） | 1件あたり | 月（1日3〜4件） |
| --- | --- | --- | --- |
| claude-sonnet-5 | $2 / $10（2026-08-31 まで。以降 $3 / $15） | 約 $0.02 | $2〜3 |
| claude-haiku-4-5 | $1 / $5 | 約 $0.007 | $1 未満 |

## 残タスク（2026-08-04 時点）

パイプラインは7段すべて自動で回っている。
[1]収集 → [2]採点 → [3]審査 → [4][5][6]納品 → [7]学習。

### いちばんの詰まり: 承認実績が0件

未審査が34件まで積み上がっている一方、**承認が0件**。ここが埋まらないと
次の3つが全部動かせない:

- `price` 軸の重み 0.05 の検証（承認実績から見直す前提）
- 学習ループの実データ通し（`learn` は定期実行に入れたが、非承認3件では
  タグが3回に届かずルール候補が立たない）
- 納品の連番（`frmg_ig003` から続く）と Drive の実運用

**人が審査する以外に前へ進めない。**

### 判断待ち（コードは出来ている）

- **台湾2件**（`hbhousing` / `century21tw`）が `enabled: false`。
  規約ページをサイト内から見つけられず**禁止の確認が取れていない**という
  前提つきなので、有効化はユーザー判断
- **収集と採点の上限**。いま collect `--limit 10`（13ソース＝1日130件）に
  対し score `--limit 40`。上限まで入ると溜まる。実際の流入を見てから
  どちらかを動かす

### 未着手

- **手動URL投入物件の画像アップロード経路**（半日規模）。手動投入は
  ページを取得しない建て付けなので、画像も人が入れる必要がある。
  60サイトのスプレッドシートを実際に使うにはここが要る
- ArchDaily / Architectural Digest を `collect --dry-run --explain` で検証
  （どちらも抜粋配信なので probe では判断できない）
- **Portland's Condos は画像の権利が未確認**。フィードの質は採用圏内
- Beverly Hills Estates（所在地が取れない）と Nest Seekers（カナダの物件を
  United States として登録する）の保留解除。後者は物件ごとの国判定が要る
- JS描画サイト向けの Playwright 組み込み（1〜2日規模）。Christie's /
  Century 21 / Hilton & Hyland / Living Room / Windermere / CB Bain の
  6件が射程に入る
- 区分A/Bの大手ポータル10件と区分Fの3件は robots 未実測。ただし方針で
  手動のみと決まっているので、測っても結論は動かない

### 完了（このセッション）

PostgreSQL移行と Secrets 登録 / 審査UIの共有（Render・Basic認証）/
納品の自動実行（launchd）/ 採点モデルの変更（Haiku 4.5）/
フィードのバックフィル / 仲介サイト4件の有効化 / 台湾2件の実装 /
所在地の必須化 / 学習ループの定期実行化 / CI の修復

## ハマりどころ（前セッションで踏んだもの）

- **記事本文は `<p>` だけを集める**（`collect/base.py` の `_body_text`）。
  class 名ベースの除去では関連記事の混入を防げなかった。安易に戻さない
- Robb Report は代表画像が顔写真入り合成のことがある →
  `skip_lead_image: true` で2枚目を使う。サムネイル選定は
  `images/extract.py` の `image_urls_from_soup` に一本化してある。
  **審査UI側と納品側でロジックを二重化しない**（一度それでバグらせた）
- フィードの鮮度は `days_since_newest` で見る。窓の長さだけ見ると
  停止済みフィードが健全に見える
- ペース測定はスキップ判定の**前に**日付を記録する（url_exclude 分を
  落とすと過小評価になる）
- SQLite/Postgres の差分は `db/dialect.py` に集約。SQL を書くときは
  `?` プレースホルダで書き、変換に任せる
- `DomainRateLimiter` はプロセス内限定。collect を並列に走らせない
- zsh ユーザーなので、案内するコマンドに `#` コメントや `<ID>` などの
  プレースホルダを混ぜない（そのまま貼って事故った実績あり）

## クラウド環境で見た目を確認するとき

Playwright + 同梱 Chromium でスクリーンショットを撮る手順。3つ踏んだ:

- `headless_shell` ではなく **`/opt/pw-browsers/chromium-*/chrome-linux/chrome`**
  を `executable_path` に指定する。`playwright install` は不要
- 起動引数に **`--ssl-version-max=tls1.2`** を入れる。TLS1.3 の
  ClientHello がエージェントプロキシで切られ、`ERR_CONNECTION_RESET` になる
- プロキシは `launch(proxy=...)` ではなく **`--proxy-server=$HTTPS_PROXY`**
  を args で渡す。`proxy=` を使うと Playwright が
  `--proxy-bypass-list=<-loopback>` を足し、127.0.0.1 の自前サーバに
  届かなくなる。**ポート番号はセッションが再起動すると変わる**ので、
  値を焼き込まず毎回 `$HTTPS_PROXY` を読む

## ユーザーとの約束事

- **確認を取ってから動く。** 特にデザイン・外部送信・破壊的操作。
  「勝手にやるなよ」と一度言われている
- 報告は率直に。失敗・未実施はそのまま伝える
- やり取りは日本語
