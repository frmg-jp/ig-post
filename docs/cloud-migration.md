# クラウド移行の計画

目的は「別の担当者がブラウザだけで審査できること」。ターミナル・ローカルDB・
ローカルの認証ファイルをなくし、収集から納品までをクラウド側で完結させる。

まだ着手していない。着手する前にこの文書の「先に決めること」を潰す。

## 現状のどこがローカルに縛られているか

| 箇所 | いまの実装 | クラウドで問題になる点 |
|---|---|---|
| DB | SQLite（`data/freming.db`） | コンテナが使い捨てなので消える。同時に触れない |
| 審査UI | 127.0.0.1 で認証なし | そのまま公開できない |
| Drive認証 | `credentials/token.json`（OAuth） | ファイルを置けない。リフレッシュが切れると対話が要る |
| 定期実行 | 人が `collect` / `score` を叩く | 誰も叩かなくなる |
| 画像の作業場所 | `data/images/` | 使い捨てで良いが、パスの前提は変える必要がある |
| レート制限 | プロセス内の `DomainRateLimiter` | **インスタンスが増えた瞬間に規約違反になる（後述）** |

## 推奨する構成

**Google Cloud Run + Cloud SQL (PostgreSQL)**。

| 役割 | 置き場所 |
|---|---|
| 審査UI | Cloud Run サービス（IAP でGoogleアカウント制限） |
| 納品ワーカー | Cloud Run サービス **min=1 / max=1** |
| 収集・採点・学習 | Cloud Run ジョブ + Cloud Scheduler |
| DB | Cloud SQL for PostgreSQL（最小構成） |
| 鍵・APIキー | Secret Manager |
| 納品先 | いまの共有ドライブのまま |

この構成を推す理由は、すでに Google 組織と共有ドライブを使っていること。

- **認証を書かなくていい**。IAP を前に置けば、指定したGoogleアカウントだけが
  審査UIに入れる。担当者を増やすのはIAMに1行足すだけ。いま審査UIに認証がないのは
  「127.0.0.1 だから」であって、公開するなら何かは要る。自前でログインを作るより安い。
- **`token.json` の問題が消える**。`drive.auth_mode` を `service_account` にする。
  サービスアカウントはマイドライブに容量を持たないが、納品先はすでに共有ドライブなので
  そのまま動く（疎通確認済みの経路）。対話的な再認証が要らなくなるので、自動納品の
  `allow_interactive=False` の制約とも噛み合う。

GCPを使わない場合の代替は **Supabase (PostgreSQL) + Render / Fly.io**。DBは楽になるが、
認証は自前で書くことになる。

## 最重要の制約：レート制限は1インスタンスでしか守れない

いまの `DomainRateLimiter` はプロセス内のロックでしかない。外向きのリクエストを出す
プロセスが2つ以上に増えた瞬間、**「同一ドメインへの並列アクセス禁止」「間隔3秒以上」が
破れる**。これは性能の話ではなく、こちらが宣言している遵守事項が守れなくなるということ。

したがって:

- 外向きにリクエストを出すもの（収集・納品ワーカー）は **max-instances=1 で固定する**。
  オートスケールさせない。設定漏れで増えるのが一番危ないので、デプロイ設定に
  コメントを残す。
- 将来どうしても並列にしたくなったら、PostgreSQL の advisory lock を使って
  ドメイン単位で直列化する。それまでは1インスタンスで足りる（1日数十件の規模）。
- 審査UIは外向きのリクエストを出さないので、増やしてよい。

## 作業の内訳

### 1. PostgreSQL への移行（一番大きい）

生SQLは `db/repository.py` に集めてあるので、そこが主戦場になる。外に出ている
`conn.execute` は13箇所（`delivery/deliver.py` 5、`images/fetch.py` 5、
`images/process.py` 2、`delivery/worker.py` 1）。移行のついでにこれも repository に寄せる。

書き換えが要るSQLiteの前提:

- `datetime('now')` → `now()`
- `INSERT OR IGNORE` → `INSERT ... ON CONFLICT DO NOTHING`
- `PRAGMA foreign_keys` / `journal_mode` → 不要
- `sqlite3.Row` → `dict`（テンプレートが `row["key"]` で読んでいるのでそのまま動く）
- `migrations/*.sql` → PostgreSQL用に書き直し。`ALTER TABLE ADD COLUMN` はほぼそのまま、
  `AUTOINCREMENT` → `GENERATED ALWAYS AS IDENTITY`

**ORMは入れない**。SQLAlchemy を挟んでも、いまの薄さに対して抽象が増えるだけで
得るものが少ない。`psycopg` で直に書き、`repository.py` の関数シグネチャは変えない。
そうすれば呼び出し側（web / delivery / scoring）はほぼ触らずに済む。

### 2. 既存データの移行

`data/freming.db` の中身を流し込む1回きりのスクリプトを書く。
`properties` / `images` / `deliveries` / `feedback` / `rule_candidates`。

**`deliveries` を必ず移すこと。** `frmg_igNNN` の連番はこのテーブルの最大値から
採っているので、移し忘れると `frmg_ig001` から振り直しになり、Drive上で衝突する。

### 3. Drive を service_account に切り替え

`config.yaml` の `drive.auth_mode: service_account` と、鍵を Secret Manager に置くだけ。
鍵ファイルをリポジトリに入れないのは今と同じ（`.gitignore` 済み）。
サービスアカウントを共有ドライブのメンバーに追加する作業が要る。

### 4. 画像の作業ディレクトリ

`images.work_dir` を `/tmp` 配下にする。コンテナは使い捨てなので、途中で落ちたら
次回また取り直す。いまも「納品記録を最後に書く」ことで取りこぼし側に倒してあるので、
考え方は変えなくていい。

### 5. 定期実行

Cloud Scheduler から Cloud Run ジョブを叩く。頻度の目安:

- `collect` … 1日1回（ソースが増えたら朝夕2回）
- `score` … `collect` の直後
- `learn` … 週1回

### 6. ログ

`logging_setup` の出力先を標準出力に寄せれば Cloud Logging がそのまま拾う。
ファイル出力はローカル実行のときだけにする。

## 先に決めること

1. **GCP か、Supabase + Render か。** 推奨はGCP（上記の理由）。
2. **担当者は審査だけをするのか。** 収集ソースの追加や重みの調整も任せるなら、
   `config.yaml` がファイルである以上、変更のたびにデプロイが要る。設定をDBに逃がすか、
   その部分だけ管理画面を作るかの判断が要る。**審査だけなら今のままでいい。**
3. **費用の上限。** ざっくり月 $20〜40 程度（Cloud SQL 最小構成 + 納品ワーカーの常駐分）。
   ワーカーを常駐させる必要があるのでゼロにはならない。安く抑えるなら Supabase の
   無料枠 + Fly.io だが、手間は増える。

## いま先回りしてやらない方がいいこと

- **SQLiteのままORMや抽象レイヤを足すこと。** 移行時に一緒にやる方が早い。
  先に抽象だけ入れても、PostgreSQL に当てるまで正しさが確かめられない。
- **審査UIに自前のログインを作ること。** IAPを使うなら丸ごと不要になる。
