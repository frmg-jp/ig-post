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

## 残タスク（トンマナの次）

優先度はユーザーに確認してから着手する:

- Robb Report の再収集（サムネイル修正後にユーザーが `remove --source
  robbreport_shelter` 済み。`collect --source robbreport_shelter` で取り直し）
- ArchDaily / Architectural Digest を `collect --dry-run --explain` で検証
  （config では disabled のまま）
- PostgreSQL 移行: **完了**（2026-08-03、Neon 無料枠 / Singapore /
  PostgreSQL 18.4、46行）。残るは GitHub Secrets への `DATABASE_URL` 登録
  だけ。詳細は docs/postgres-migration.md
- 手動URL投入物件の画像アップロード経路（未実装・半日規模。手動投入物件は
  こちらからページを取得しない建て付けなので、画像も人が入れる必要がある）
- 仲介サイトの自動収集（経路A）: dreamtown / vanguard の2件を通した。
  残りは docs/idx-survey.md を見る。有効化は `enabled: true` にするだけ
- `price` 軸の重み 0.05 は未検証。承認実績が溜まってから見直す
- 学習ループ（`learn` → ルール候補UI）は実データで未通し

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
