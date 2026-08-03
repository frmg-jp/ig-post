# PostgreSQL 移行の手順（2026-08-03）

定期実行（`.github/workflows/collect.yml`、毎日 09:00 JST）と審査UIが
同じデータを見るために、SQLite から PostgreSQL へ移す。

**いまここが未了だと、毎日の収集はランナーの使い捨て SQLite に書いて
毎回消える。** ワークフローは `DATABASE_URL` を GitHub Secrets から読む
建て付けになっているので、そこが空だと config の `db_path`（ランナー内の
`data/freming.db`）に落ちる。収集先をいくら有効にしても積み上がらない。

## 現在の状況

**移行済み（2026-08-03）。** Neon の無料枠、Singapore リージョン、
PostgreSQL 18.4。properties 23 / feedback 1 / deliveries 2 / images 20 の
計46行を移し終えている。

残るのは GitHub Secrets への `DATABASE_URL` 登録だけ。ここが空だと
定期実行はランナーの使い捨て SQLite に書いて毎回消える。

### 経緯

Supabase は見送った。無料枠が1ユーザー2プロジェクトまでで、
`yadokari-quotation` と `akiyax` で埋まっており、2026-08-03 に
`freming-curated` の作成を試みて弾かれた。

```
akiyax.jp@gmail.com (2 project limit), isseisawada@gmail.com (2 project limit)
```

Neon の無料枠は1 Organization に **100プロジェクト**まで作れるので、
既存を止めずに済む。実測に基づく見積もり:

| 項目 | Neon Free | このプロジェクトの見込み |
| --- | --- | --- |
| ストレージ | 0.5 GB / プロジェクト | 1件あたり約3KB（`content_text` 実測 平均2,366字）。15万件相当 |
| コンピュート | 100 CU時間 / 月 | 5分アイドルで自動停止。毎日の収集と審査で月10 CU時間程度 |
| 転送量 | 5 GB / 月 | 画像はDBに入らない（Drive送り）ので問題にならない |

注意点:

- 5分アイドルで停止するので、審査UIを開いた最初の1回だけ起動待ちが入る
- Neon に東京リージョンは無い。日本から最も近いのは Singapore
- 履歴保持は6時間・手動スナップショット1つ。定期バックアップは別途考える

## 手順

### 1. 移行先を用意する

Neon でプロジェクトを作り、Connection string を控える。
`postgresql://USER:PASSWORD@ep-xxxx.REGION.aws.neon.tech/neondb?sslmode=require`
の形。pooler 付き・直結のどちらでもよい（下記「prepared statement」を参照）。

### 2. 接続を確かめる

移行は1回きりなので、先に接続文字列だけ試す。

```
export DATABASE_URL='ここに接続文字列'
python -m freming.cli db check --db "$DATABASE_URL"
```

繋がれば、サーバのバージョン・マイグレーションの適用状況・各テーブルの
行数が出る。「空です。db transfer の移行先として使えます。」と出れば次へ。

### 3. 移行する

移行元は config の `db_path`、移行先は `DATABASE_URL`。

```
export DATABASE_URL='ここに接続文字列'
python -m freming.cli db transfer
```

マイグレーションの適用まで含めて中で行う。移行先が空でないと止まるので、
取り違えても二重投入にはならない。終わると各テーブルの行数が出る。

### 4. GitHub Secrets に登録する

リポジトリの Settings → Secrets and variables → Actions で、
`DATABASE_URL` に同じ接続文字列を登録する。ワークフロー側の修正は不要
（すでに `secrets.DATABASE_URL` を読んでいる）。

### 5. 審査UIを Postgres に向ける

`.env` に `DATABASE_URL` を書いておけば `serve` も `collect` も同じDBを見る。

## 検証済みの内容（2026-08-03、PostgreSQL 16）

ローカルに立てた PostgreSQL 16 に対して、上の手順をそのまま通した結果:

- 移行 16行（properties 9・feedback 7）。マイグレーション6件も自動適用
- **二重実行は止まる**。移行先に行があると「空のデータベースに対して
  実行してください」で失敗する
- **移行後の採番が続く**。IDENTITY を最大 id に合わせているので、
  移行直後の登録が id=10 になった（主キー衝突しない）
- 審査UI・収集とも Postgres 相手で動作
- `tests/test_postgres.py` の7件（普段はスキップ）が全て通る
- `db check` が空のDBと中身のあるDBを見分ける
- 移行後に `collect` を回して6件が追記できた

本番（Neon / PostgreSQL 18.4）でも同じ手順が通り、**`deliveries` の2行が
移った**ことを確認した。ここを取りこぼすと frmg_igNNN が振り直しになり、
Drive 上の既存フォルダと衝突する。次の納品は frmg_ig003 から続く。

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
- **prepared statement とプーラ。** psycopg は同じSQLを5回実行すると自動で
  サーバ側 prepared statement に切り替える。接続プーラをトランザクション
  単位で使う構成（Neon の `-pooler` エンドポイント、Supabase の 6543番
  ポート）では、次の実行が別の接続に振られて落ちる。収集も審査UIも同じSQLを
  繰り返すので確実に踏む。`connection.py` で `prepare_threshold=None` を
  渡して無効にしてあるため、pooler 付き・直結のどちらの接続文字列でも動く
