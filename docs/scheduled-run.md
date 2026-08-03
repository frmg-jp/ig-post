# 定期実行のセットアップ（Supabase + GitHub Actions）

目的は **収集と採点をMacから切り離すこと**。審査UIと納品はMacに残したまま、
毎日の収集・採点はクラウドで回る。

```
GitHub Actions（毎日 09:00 JST）
   └─ collect → score ──> Supabase (PostgreSQL)
                              ↑
                     Mac の審査UI・納品がここを見る
```

## なぜDBを移すのが前提なのか

収集をクラウドで回しても、その結果を審査UIが見られないと意味がない。
SQLite が Mac のディスクにある限りクラウド側は書き込めないので、
**「定期実行をMacから外す」＝「DBをMacから外す」**になる。

逆に言えばDBさえ動かせば、審査UIと納品はMacのままでよい。
Actions が必要とする秘匿値も `DATABASE_URL` と `ANTHROPIC_API_KEY` だけで済み、
**Driveの鍵をクラウドに置かずに済む**（納品はMacに残るため）。

## 手順

### 1. Supabase でデータベースを作る

1. https://supabase.com/ でプロジェクトを作成（リージョンは Tokyo）
2. Project Settings → Database → Connection string → **URI** をコピー
3. `[YOUR-PASSWORD]` を実際のパスワードに置き換える

接続文字列は `postgresql://postgres.xxxx:パスワード@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres`
のような形になる。

### 2. 手元の .env に書く

```
DATABASE_URL=postgresql://postgres.xxxx:パスワード@....supabase.com:5432/postgres
```

`config.yaml` には**書かない**（パスワードが入るため）。`DATABASE_URL` が
設定されていればそちらが、無ければ `app.db_path` の SQLite が使われる。

### 3. 依存を入れる

```bash
pip install -e ".[postgres]"
```

### 4. いまのデータを移す

```bash
python -m freming.db.transfer --from data/freming.db --to "$DATABASE_URL"
```

移行先のマイグレーションは自動で適用される。移行後に件数を突き合わせ、
合わなければ失敗として止まる。

**`deliveries` が移ることが特に重要。** `frmg_igNNN` の連番はこのテーブルの
最大値から採っているので、取りこぼすと `frmg_ig001` から振り直しになり、
Drive 上で既存フォルダと衝突する。テストでもそこを確かめている。

移行は**空のデータベースに対してのみ**実行できる。やり直すときは
Supabase 側でテーブルを消してから。

### 5. 確認

```bash
python -m freming.cli status      # 移行前と同じ件数が出るか
python -m freming.cli serve       # 審査UIが Supabase を見ている
```

`.env` に `DATABASE_URL` があれば、`serve` も `deliver` も自動でそちらを見る。
SQLite に戻したいときは `DATABASE_URL` をコメントアウトするだけ。

### 6. GitHub のシークレットを登録

リポジトリの Settings → Secrets and variables → Actions:

| 名前 | 値 |
|---|---|
| `DATABASE_URL` | 手順1の接続文字列 |
| `ANTHROPIC_API_KEY` | 採点に使うAPIキー |

Drive の鍵は登録しない（納品はMacで行うため）。

### 7. 動かす

`.github/workflows/collect.yml` が毎日 09:00 JST に走る。
初回は Actions タブから **Run workflow** で手動実行して確かめる。

## 守っていること

- **`concurrency: freming-collect` で重複起動を止める。** 前の実行が終わる前に
  次が走ると、同一ドメインへの並列アクセスになって収集ポリシーが破れる。
  これは性能の話ではなく、こちらが宣言している遵守事項が守れなくなるということ。
- **1ソースが落ちても残りを止めない。** 相手サイト側の一時的な不調で全体が
  止まると、翌日まで何も入らなくなる。
- **納品はワークフローに含めない。** Drive の鍵を置く先を1か所に保つため。

## 費用

| | 月額 |
|---|---|
| Supabase | 無料枠（この規模なら十分） |
| GitHub Actions | 無料枠（collect + score で1日3〜5分、月150分程度） |

## SQLite と PostgreSQL の両対応について

テストは SQLite で完結させている（DBサーバー不要で速い）。ただしそれだけだと
「SQLite では通るが PostgreSQL で落ちる」書き方が入り込むので、
`tests/test_postgres.py` が実接続でも確かめる。CI では両方走る。

差は `src/freming/db/dialect.py` に閉じ込めてある。SQLを書くときの約束:

- プレースホルダは `?`（PostgreSQL 用に `%s` へ変換される）
- `INSERT OR IGNORE` と `lastrowid` は使わない
  → `INSERT ... ON CONFLICT DO NOTHING RETURNING id`
- 時刻の比較に `datetime('now', ...)` を使わない
  → Python 側で計算してパラメータで渡す

3つめは実際にバグを踏んだところ。`_now()` の ISO 形式（`2026-08-03T…`）と
`datetime('now')` の形式（`2026-08-03 …`）は区切り文字が違うため、
文字列比較が常に偽になっていた。
