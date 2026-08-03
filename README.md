# FREMING CURATED

世界中の「今、購入可能な気になる建築」を発掘し、人間が承認したものだけを画像加工して
Google Drive に納品するパイプライン。

```
[1] 収集 → [2] 判定 → [3] 審査UI → [4] 画像取得 → [5] 加工 → [6] 納品
                          ↓ 非承認 → 理由を記録し次回のスコアリングに反映
```

## セットアップ

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # ANTHROPIC_API_KEY を設定
# Drive の認証情報を配置（下記「Drive の認証」を参照）

python -m freming.cli db migrate     # [1] DBスキーマ
python scripts/check_drive.py        # [2] Drive疎通確認（★ここが通るまで先へ進まない）
```

## コマンド

| コマンド | 内容 |
|---|---|
| `python scripts/check_drive.py` | Drive の書き込み権限を実際に検証（テストファイルを作成→削除） |
| `python -m freming.cli check-drive` | 同上 |
| `python -m freming.cli db migrate` | マイグレーション適用（再実行は安全） |
| `python -m freming.cli db status` | 適用状況の表示 |
| `python -m freming.cli collect --source dezeen --limit 10 [--dry-run]` | 編集ソースから収集（経路B） |
| `python -m freming.cli discover-feed <サイトURL>` | トップページから公開フィードURLを探す |
| `python -m freming.cli probe-feed <フィードURL>` | フィードの中身を試す（DBに書き込まない） |
| `python -m freming.cli ingest-url <URL>` | URLを1件だけ取得して候補化（Zillow等はこの経路のみ） |
| `python -m freming.cli check-api` | スコアリングAPIの疎通確認 |
| `python -m freming.cli score [--limit 20] [--dry-run]` | 未採点の候補をスコアリング（[2]） |
| `python -m freming.cli serve` | 審査UIを起動（[3]）。承認したものはそのまま納品まで進む |
| `python -m freming.cli deliver [--limit 5] [--dry-run]` | 承認済みを画像取得→加工→Drive納品（[4][5][6]） |
| `python -m freming.cli deliver --watch` | 承認済みを拾い続ける常駐モード（`serve` を起動していれば不要） |
| `python -m freming.cli learn` | 非承認理由を分類しルール候補を作る（[7]） |
| `python -m freming.cli rules list \| approve <タグ> \| dismiss <タグ>` | ルール候補の確認と承認（[7]） |
| `python -m freming.cli reset-images --id <ID>` | 取得済み画像を捨てて取り直す（抽出ルールを直したとき） |
| `python -m freming.cli status` | 候補の件数をステータス別に表示 |
| `python -m freming.collect.editorial --source dezeen` | 同上（モジュール単体実行） |
| `python -m freming.scoring.runner --limit 20` | 同上（モジュール単体実行） |
| `python -m freming.delivery.deliver --dry-run` | 同上（モジュール単体実行） |
| `python -m pytest tests/ -q` | テスト |

設定値はすべて `config.yaml`。秘匿値のみ `.env`。

## 承認から納品までの自動化

`serve`（審査UI）の中でワーカーが動き、審査UIで承認したものを順に納品します。
別ターミナルで `deliver` を実行する必要はありません。

```
承認 → 画像取得 → 正方形加工 → frmg_igNNN を作成 → Drive へアップロード
```

進み具合は審査UIに出ます。ヘッダーに「自動納品 ON・待ち N」、
カードには「納品待ち」「納品中…」、納品後は「Drive で開く」。
承認タブと納品済タブは、待ちがある間だけ15秒ごとに自動更新されます
（未審査タブは審査の邪魔になるので更新しません）。

止まるべきところで止まるようにしてあります。

- 納品は**1件ずつ直列**。画像取得は相手サイトへのアクセスなので、収集と同じ
  「間隔3秒以上・同一ドメインへの並列アクセス禁止」がそのまま適用されます。
- 失敗は `delivery.max_attempts` 回まで。超えたら自動では触らず、審査UIの
  **「納品を再試行」**から人が再開します。取れない画像を取りに行き続けません。
- 失敗しても**承認済みのまま**残ります（一覧から消えると追跡できないため）。
  失敗理由はカードに出ます。
- Drive の認証が切れていても、同意画面（ブラウザ）には進みません。
  `python -m freming.cli check-drive` で認証し直してください。

`config.yaml`:

```yaml
delivery:
  auto: true              # false にすると従来どおり deliver を手で実行する
  poll_interval_sec: 30   # 承認漏れの拾い直し間隔（承認直後は待たずに始まる）
  batch_limit: 5          # 1巡で納品する最大件数
  max_attempts: 3         # 自動での試行回数の上限
  retry_after_sec: 600    # 失敗した候補を次に試すまでの待ち時間
```

## Drive の認証

`config.yaml` の `drive.auth_mode` で3方式から選べます。**既定は `oauth`**。

| モード | 用途 |
|---|---|
| `oauth` | OAuthクライアントで人のアカウントとして認証。**サービスアカウント鍵の作成が組織ポリシーで禁止されている場合はこれ** |
| `service_account` | サービスアカウントのJSON鍵。無人実行向けだが、鍵の作成が許可されている必要がある |
| `adc` | gcloud のログイン / Workload Identity 連携 / サービスアカウントの権限借用 |

### oauth（既定）

1. [Google Cloud Console](https://console.cloud.google.com/) →「APIとサービス」→「ライブラリ」
   で **Google Drive API** を有効化
2. 「OAuth 同意画面」を設定（User Type は組織内なら「内部」。スコープの事前追加は不要）
3. 「認証情報」→「認証情報を作成」→「OAuth クライアント ID」→ アプリケーションの種類
   **「デスクトップアプリ」**
4. ダウンロードしたJSONを `credentials/oauth_client.json` に配置
5. `python scripts/check_drive.py` を実行 → ブラウザで同意 → `credentials/token.json` が
   自動生成される（以降は対話なしで動作）

このモードではログインした本人としてファイルが作成されるため、保存容量は本人のもの
が使われ、マイドライブ・共有ドライブのどちらでも動作します。

### service_account

1. 「認証情報」→「サービスアカウント」を作成（ロールの割り当ては不要）
2. 「鍵」タブ →「鍵を追加」→ **JSON**（ダウンロードは1回限り）
3. `credentials/service-account.json` に配置し、`auth_mode: service_account` に変更
4. **納品先の共有ドライブにそのメールアドレスをメンバー追加し、「コンテンツ管理者」以上にする**

> 「サービス アカウント キーの作成をブロックする組織ポリシーが適用されています」と出る
> 場合、`iam.disableServiceAccountKeyCreation` が有効です。`oauth` か `adc` を使ってください。

### adc

```bash
gcloud auth application-default login \
  --scopes="https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/cloud-platform"
```

サービスアカウントとして動かしたいが鍵を作れない場合は、権限借用（`roles/iam.serviceAccountTokenCreator`）
と組み合わせられます。

> `credentials/` 配下（鍵・トークン）はパスワードと同等です。`.gitignore` 済みですが
> コミット・共有しないでください。

## Drive 納品の設定（重要）

`scripts/check_drive.py` は次を順に実行します。

1. サービスアカウント鍵の読み込み（**メールアドレスを表示** — 共有先の照合に使う）
2. サービスアカウントの保存容量
3. 納品先フォルダの素性（共有ドライブか / `canAddChildren` があるか）
4. テキストファイル作成 → `files.get` でサイズ検証 → 削除
5. サブフォルダ作成 → 1080×1080 JPEG をアップロード → サイズ検証 → 後片付け
6. 既存 `frmg_ig*` フォルダの中身を点検（**空フォルダを検出**）

### 「フォルダは作られるのに画像が入らない」症状について

原因は 2 通りあり、どちらも 4/5 の実書き込みテストで切り分けられます。

| 原因 | 症状 | 対処 |
|---|---|---|
| フォルダがサービスアカウントに共有されていない | フォルダ作成の時点で 403 | 対象フォルダをサービスアカウントのメールアドレスに「編集者」として共有 |
| **納品先がマイドライブ配下** | フォルダ（0バイト）は作成できるが、ファイルのアップロードだけが `storageQuotaExceeded` で失敗 | 納品先を**共有ドライブ（Shared Drive）**に作り直し、`drive.shared_drive_id` を設定 |

サービスアカウントは**マイドライブに保存容量を持ちません**。個人のマイドライブ配下に
書き込もうとすると、容量を消費しないフォルダだけが作られ、画像は 1 枚も入りません。
共有ドライブではファイルの所有者がドライブ側になるため、この制限を受けません。

## 実装状況

- [x] 1. DBスキーマとマイグレーション
- [x] 2. Drive疎通確認スクリプト
- [x] 3. 収集モジュール（編集ソース1つ、RSS経由）
- [x] 4. スコアリング
- [x] 5. 審査UI
- [x] 6. 画像取得・加工・納品
- [x] 7. 学習ループ

## スコアリングの仕組み

最終スコアは6つの軸の加重和です。重みは `config.yaml` の `scoring.weights`。

| 軸 | 決め方 |
|---|---|
| story | Claude が判定（前歴の可視性を最重要とする） |
| source | 取得元のランク（S/A/B）から機械的に |
| for_sale | 収集時のシグナル検出と Claude の読みの一致度 |
| genre | `genres.priority` の順位から機械的に |
| area | `focus_areas` との一致。外れても0点にはせず足切りしない |
| price | 価格が判明しているか（金額の高低は見ない） |

Claude に任せるのは story の判定と属性抽出だけで、合算は Python 側で行います。
軸ごとの点数は `properties.score_detail` に JSON で残るため、重みを変えた場合は
APIを呼び直さずに再計算できます。

判定基準は `docs/approval-criteria.md`（承認済み8件の分析）が根拠で、その要点を
`scoring.approved_examples` と `scoring.approval_notes` からプロンプトに載せています。
基準を変えるときはコードではなく `config.yaml` を編集してください。

## 学習ループ

```
不承認（理由必須） → feedback に蓄積 → learn でタグ分類
    → 同じタグが N 件でルール候補として提示 → 人が承認 → 次回のプロンプトへ
```

ルールは**自動適用しません**。「3回同じ指摘が出た」ことと「今後それを恒久的に
除外してよい」ことは別なので、審査UIの「ルール候補」画面か
`freming rules approve <タグ>` で必ず人の承認を挟みます。却下したタグは
件数が増えても再提案しません。

タグの語彙は `config.yaml` の `scoring.feedback.tags`。表記のゆれを吸収して
集計するための固定語彙で、どれにも当てはまらない理由は `other` に寄せます
（`other` はまとめてルール化しません）。

## 連載企画（series）

「FREMING Pick」「Hidden Gem」などの企画にどれを載せるかは編集判断であり、
記事本文から機械的には決まりません。**スコアリングでは判定せず、審査UIで人が
選びます。** 一覧はこのラベルで絞り込めます。

企画の増減は `config.yaml` の `series` を編集してください。`key` はDBに保存
される値なので変更しないでください（既存の行が孤立します）。`label` は表示名
なので自由に変えられます。納品する `meta.txt` には `label` の方が入ります。

納品済みのラベルは変更できません。`meta.txt` は納品時に書き出しているため、
あとからラベルだけ変えると Drive の内容と食い違うためです。

## アクセスポリシー

`config.yaml` の `http` セクションは「守るための下限」で、緩める方向の変更は
`config.py` のバリデーションで拒否されます。

- robots.txt の Disallow を尊重（`respect_robots_txt: false` は設定不可）
- 同一ドメインへのリクエスト間隔 3 秒以上、並列アクセス禁止
- User-Agent に連絡先を明記（未記載は起動時エラー）
- Zillow / Redfin / Compass は `mode: manual_only`。自動収集の対象から必ず除外され、
  管理画面の手動URL投入のみを受け付けます
- ログイン・CAPTCHA回避・レート制限回避の実装は行いません
