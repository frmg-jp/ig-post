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
# credentials/service-account.json にサービスアカウント鍵を配置

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
| `python -m freming.db.migrate --status` | 同上（モジュール単体実行） |
| `python -m pytest tests/ -q` | テスト |

設定値はすべて `config.yaml`。秘匿値のみ `.env`。

## サービスアカウント鍵の用意

1. [Google Cloud Console](https://console.cloud.google.com/) で「APIとサービス」→「ライブラリ」
   から **Google Drive API** を有効化
2. 「認証情報」→「認証情報を作成」→「サービスアカウント」で作成（ロールの割り当ては不要。
   Drive の権限は Drive 側の共有設定で決まる）
3. 作成したサービスアカウント →「鍵」タブ →「鍵を追加」→「新しい鍵を作成」→ **JSON**
   （ダウンロードは1回限り）
4. ダウンロードした JSON を `credentials/service-account.json` にリネームして配置

   ```bash
   mv ~/Downloads/<プロジェクト名>-xxxxxxx.json credentials/service-account.json
   python -c "import json; print(json.load(open('credentials/service-account.json'))['client_email'])"
   ```

5. **納品先の共有ドライブに、上で表示されたメールアドレスをメンバー追加し、役割を
   「コンテンツ管理者」以上にする**（これを忘れるとフォルダは作れても画像が入らない）
6. `python scripts/check_drive.py` で検証

> 鍵はパスワードと同等です。`.gitignore` 済みですがコミット・共有しないでください。
> 漏洩時は Cloud Console の「鍵」タブから削除して作り直せます。

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
- [ ] 3. 収集モジュール（編集ソース1つ、RSS経由）
- [ ] 4. スコアリング
- [ ] 5. 審査UI
- [ ] 6. 画像取得・加工・納品
- [ ] 7. 学習ループ

## アクセスポリシー

`config.yaml` の `http` セクションは「守るための下限」で、緩める方向の変更は
`config.py` のバリデーションで拒否されます。

- robots.txt の Disallow を尊重（`respect_robots_txt: false` は設定不可）
- 同一ドメインへのリクエスト間隔 3 秒以上、並列アクセス禁止
- User-Agent に連絡先を明記（未記載は起動時エラー）
- Zillow / Redfin / Compass は `mode: manual_only`。自動収集の対象から必ず除外され、
  管理画面の手動URL投入のみを受け付けます
- ログイン・CAPTCHA回避・レート制限回避の実装は行いません
