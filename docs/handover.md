# 引き継ぎ（2026-08-03）

前セッション（環境: デフォルト）から、ネットワーク制限のない環境
「ネットオープン」への移行用。**最初にこのファイルを読むこと。**

## 作業ブランチ

`claude/freming-curated-pipeline-mfzomz`（このファイルを含む最新をプッシュ済み）。
開発・プッシュはこのブランチのみ。他ブランチへは押さない。

## いますぐやるタスク: 審査ページのトンマナ合わせ

依頼原文:
「審査ページを以下FREMINGの公式ページのデザインにトンマナを合わせて。
フォントも再調整。 https://frmg.jp/」

前セッションは frmg.jp に到達できず（ゲートウェイ403）、**現物を見ずに**
「編集メディア風」の仮デザインを実装した（コミット `cd3ed95`）。
ユーザーから「勝手にやるな」と指摘があり、仮デザインは残す判断になったが
**frmg.jp の実物との突き合わせは未実施**。この環境では到達できるはずなので:

1. https://frmg.jp/ を取得し、トップと記事一覧の配色・フォント・余白・
   角丸・字送りを読み取る（CSS も見る。`body` の `font-family` は必ず確認）
2. `src/freming/web/templates/base.html` **冒頭の `:root` ブランドトークン**
   （`--bg` `--fg` `--hi` `--radius` `--sans` など）を差し替える。
   トンマナ調整がここだけで済むよう設計してある。トークンの外を触るのは
   構造上の必要がある場合だけにする
3. ダークモード（`prefers-color-scheme: dark`）を残すかは frmg.jp 次第。
   ライトのみのサイトなら削除も提案する
4. **見た目の確認はスクリーンショットで**。Playwright + 同梱 Chromium
   （実行ファイル: `/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell`
   を `executable_path` に指定。`playwright install` は不要）で
   ダミーデータの一覧を描画して画像をユーザーに見せ、**承認を得てから**
   コミットする。前セッションの反省点: 確認前に押して叱られた

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

## 残タスク（トンマナの次）

優先度はユーザーに確認してから着手する:

- Robb Report の再収集（サムネイル修正後にユーザーが `remove --source
  robbreport_shelter` 済み。`collect --source robbreport_shelter` で取り直し）
- ArchDaily / Architectural Digest を `collect --dry-run --explain` で検証
  （config では disabled のまま）
- Supabase 移行: ユーザーのアカウントでプロジェクト作成 →
  `python -m freming.cli db transfer` → GitHub Secrets に DSN 設定 →
  `.github/workflows/collect.yml`（毎日0時UTC）が Postgres を向くようにする
- 手動URL投入物件の画像アップロード経路（未実装・半日規模。手動投入物件は
  こちらからページを取得しない建て付けなので、画像も人が入れる必要がある）
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

## ユーザーとの約束事

- **確認を取ってから動く。** 特にデザイン・外部送信・破壊的操作。
  「勝手にやるなよ」と一度言われている
- 報告は率直に。失敗・未実施はそのまま伝える
- やり取りは日本語
