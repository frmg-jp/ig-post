# PostgreSQL 移行の手順（2026-08-03）

定期実行（`.github/workflows/collect.yml`、毎日 09:00 JST）と審査UIが
同じデータを見るために、SQLite から PostgreSQL へ移す。

**いまここが未了だと、毎日の収集はランナーの使い捨て SQLite に書いて
毎回消える。** ワークフローは `DATABASE_URL` を GitHub Secrets から読む
建て付けになっているので、そこが空だと config の `db_path`（ランナー内の
`data/freming.db`）に落ちる。収集先をいくら有効にしても積み上がらない。

## 現在の状況

**プロジェクト作成で止まっている。** Supabase の無料枠は1ユーザーあたり
2プロジェクトまでで、`yadokari-quotation` と `akiyax` で埋まっている。
2026-08-03 に `freming-curated`（ap-northeast-1、月額 $0）の作成を試みて
以下で弾かれた。

```
akiyax.jp@gmail.com (2 project limit), isseisawada@gmail.com (2 project limit)
```

選択肢は3つ。いずれもユーザーの判断:

1. 既存プロジェクトのどれかを一時停止（pause）する
2. 既存プロジェクトに相乗りし、`freming` スキーマを切って同居させる
3. Pro プランに上げる（月額が発生する）

移行処理そのものは PostgreSQL 16 に対して検証済み（下記）なので、
接続先さえ決まれば残りはコマンド2本で終わる。

## 手順

### 1. 移行先を用意する

Supabase のプロジェクトを作り、Connection string（Session pooler 推奨）を
控える。`postgresql://postgres.xxxx:PASSWORD@aws-...pooler.supabase.com:5432/postgres`
の形。

### 2. 移行する

移行元は config の `db_path`、移行先は `DATABASE_URL`。

```
export DATABASE_URL='ここに接続文字列'
python -m freming.cli db transfer
```

マイグレーションの適用まで含めて中で行う。移行先が空でないと止まるので、
取り違えても二重投入にはならない。終わると各テーブルの行数が出る。

### 3. GitHub Secrets に登録する

リポジトリの Settings → Secrets and variables → Actions で、
`DATABASE_URL` に同じ接続文字列を登録する。ワークフロー側の修正は不要
（すでに `secrets.DATABASE_URL` を読んでいる）。

### 4. 審査UIを Postgres に向ける

`.env` に `DATABASE_URL` を書いておけば `serve` も `collect` も同じDBを見る。

## 検証済みの内容（2026-08-03、PostgreSQL 16）

ローカルに立てた PostgreSQL 16 に対して通した結果:

- 移行 16行（properties 9・feedback 7）。マイグレーション6件も自動適用
- **二重実行は止まる**。移行先に行があると「空のデータベースに対して
  実行してください」で失敗する
- **移行後の採番が続く**。IDENTITY を最大 id に合わせているので、
  移行直後の登録が id=10 になった（主キー衝突しない）
- 審査UI・収集とも Postgres 相手で動作
- `tests/test_postgres.py` の7件（普段はスキップ）が全て通る

## 踏んだ落とし穴

- **`FREMING_TEST_DSN` を移行先と同じDBに向けない。** `test_postgres.py`
  はテーブルを作り直すので、移行したデータが消える。実際に消して
  やり直した。検証用には別のデータベースを作ること

  ```
  createdb freming        # 本番用
  createdb freming_test   # テスト用（FREMING_TEST_DSN はこちら）
  ```

- **接続文字列を画面やログに出さない。** `db transfer` は `redact()` を
  通してから表示する。パスワードが混ざる値なので、案内するときも
  `export DATABASE_URL='...'` の形で伝えて中身は書かない
- `psycopg` は `pip install -e ".[postgres]"` で入る。素の `pip install -e .`
  だけだと `ModuleNotFoundError: No module named 'psycopg'` になる
