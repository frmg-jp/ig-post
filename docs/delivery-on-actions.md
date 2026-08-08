# 納品を GitHub Actions へ移す（2026-08-07）

チームで運用するため、**特定の Mac に依存しない**ようにする。

納品だけが手元に残っていた。認証が `auth_mode: oauth` で、ブラウザで
認証した人・その端末に紐づいた `credentials/token.json` を使っていたため。

## 何を選んだか

**Workload Identity 連携。長期の鍵をどこにも置かない。**

GitHub が発行する OIDC トークンを Google 側が検証し、その場限りの
資格情報に交換する。サービスアカウントのJSON鍵を作らないので、
Secrets に貼る鍵も、流出しうる鍵も存在しない。

比較したもの:

| 方式 | 鍵の置き場 | 判断 |
| --- | --- | --- |
| oauth（現状） | 人の端末 | **チームで使えない** |
| サービスアカウント鍵 | Secrets にJSON | 鍵が存在する。組織ポリシーで作れない場合もある |
| **Workload Identity 連携** | **どこにも無い** | **採用** |

## 準備（Google Cloud 側で1回だけ）

`PROJECT_ID` と `PROJECT_NUMBER` は自分のものに置き換えること。
リポジトリは `frmg-jp/ig-post`。

```
gcloud config set project PROJECT_ID

gcloud services enable iamcredentials.googleapis.com drive.googleapis.com

gcloud iam service-accounts create freming-deliver \
  --display-name="FREMING CURATED 納品"

gcloud iam workload-identity-pools create github \
  --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='frmg-jp/ig-post'"
```

`--attribute-condition` を**必ず付けること**。これが無いと、GitHub の
どのリポジトリからでもこのサービスアカウントを借りられてしまう。

続いて、このリポジトリからだけ借りられるようにする。

```
gcloud iam service-accounts add-iam-policy-binding \
  freming-deliver@PROJECT_ID.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/frmg-jp/ig-post"
```

## 共有ドライブ側の設定

**サービスアカウントを共有ドライブのメンバーに追加する。** これを忘れると
認証は通るのに書けない（ここが一番よく詰まる）。

Google Drive で対象の共有ドライブを開き、メンバー管理から
`freming-deliver@PROJECT_ID.iam.gserviceaccount.com` を
**「投稿者」以上**で追加する。

共有ドライブを使っているので、サービスアカウント自身の容量は消費しない
（マイドライブに置く場合と違い、容量制限の問題が起きない）。

## GitHub 側の設定

リポジトリの Settings → Secrets and variables → Actions に2つ足す。
**どちらも秘密の値ではない**（鍵ではなく、識別子）。

| 名前 | 値 |
| --- | --- |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/github` |
| `GCP_SERVICE_ACCOUNT` | `freming-deliver@PROJECT_ID.iam.gserviceaccount.com` |

## 確認

Actions から「納品」を手動実行する。最初のステップに
**Drive の疎通確認**が入れてあるので、権限が足りなければ納品を1件も
中途半端にせずに落ちる。

## 手元の設定は変えていない

`config.yaml` の `auth_mode` は `oauth` のまま。ワークフロー側で
環境変数 `FREMING_DRIVE_AUTH_MODE: adc` を渡して上書きしている。

**config.yaml を書き換えると、手元と Actions のどちらかが必ず壊れる。**
移行が済んで手元で納品しなくなったら、`auth_mode: adc` に寄せて
この環境変数を消してよい。

## 動く場所の一覧（移行後）

| 仕事 | どこで | 頻度 |
| --- | --- | --- |
| 収集・採点・学習・予定作成 | GitHub Actions | 毎朝 09:00 JST |
| **納品** | **GitHub Actions** | **日中1時間おき** |
| 通常投稿・ストーリーズ・画像配信 | Render（審査UI） | 予定の時刻 |
| 週次リール | GitHub Actions | 月曜 19:00 JST |
| 再採点・本文の後追い・各種確認 | GitHub Actions（手動実行） | 必要なとき |

**Mac は要らなくなる。** 手元で `serve` を上げる必要も無い。

## 納品が1時間遅れることについて

承認してから最大1時間、Drive にフォルダができない。

投稿の予定は3日先まで作る（`instagram.plan_days`）ので、投稿には
影響しない。急ぐときは Actions から「納品」を手動実行すれば、
その場で走る。

深夜（JST 0:00〜8:00）は回していない。承認が起きない時間帯に
1時間おきで叩いても、実行時間を使うだけになる。
