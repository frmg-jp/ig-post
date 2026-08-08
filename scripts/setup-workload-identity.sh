#!/usr/bin/env bash
#
# 納品を GitHub Actions から動かすための Google Cloud 側の設定。
# **1回だけ実行する。** 何度実行しても壊れない（既にあるものは作り直さない）。
#
#   bash scripts/setup-workload-identity.sh
#
# 長期の鍵は作らない。GitHub が発行する OIDC トークンを Google が検証して、
# その場限りの資格情報に交換する形にする。したがって、このスクリプトが
# 出力するのは鍵ではなく識別子だけで、漏れても悪用できない。
#
# 詳しい背景は docs/delivery-on-actions.md。

set -euo pipefail

REPO="frmg-jp/ig-post"
SA_NAME="freming-deliver"
POOL="github"
PROVIDER="github"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n%s\n' "$*" >&2; exit 1; }

command -v gcloud >/dev/null 2>&1 || die \
"gcloud が入っていません。先に入れてください。

    brew install --cask google-cloud-sdk

入れたらターミナルを開き直してから、もう一度このスクリプトを実行してください。"

gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | grep -q . || die \
"Google にログインしていません。

    gcloud auth login

を実行してから、もう一度このスクリプトを実行してください。"

ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n1)"

list_projects_or_note() {
  if ! gcloud projects list --format='table(projectId, name)' 2>/dev/null | grep -q .; then
    printf '（%s から見えるプロジェクトは1つもありません）\n' "$ACCOUNT"
  fi
}

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  say "プロジェクトが選ばれていません。${ACCOUNT} から使えるものは次のとおりです。"
  list_projects_or_note
  die "使うものを決めて、次を実行してから、もう一度このスクリプトを実行してください。

    gcloud config set project ここにプロジェクトID"
fi

# 「設定されているが、いまのアカウントからは見えない」場合がある。gcloud の
# 既定プロジェクトは別案件のまま残りやすいので、生のエラーで落とさず案内する。
if ! PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' 2>/dev/null)" \
   || [ -z "$PROJECT_NUMBER" ]; then
  say "いま選ばれているプロジェクト「${PROJECT_ID}」に ${ACCOUNT} からアクセスできません。"
  printf '別案件のプロジェクトが gcloud の既定として残っているか、アカウントが違います。\n'
  say "${ACCOUNT} から使えるプロジェクトは次のとおりです。"
  list_projects_or_note
  die "この中に FREMING CURATED 用のもの（Drive の OAuth クライアントを作ったもの）が
あれば、次を実行してから、もう一度このスクリプトを実行してください。

    gcloud config set project ここにプロジェクトID

一覧が空、または見当たらない場合は、そのプロジェクトを持っている別のアカウントで
ログインし直してから、もう一度実行してください。

    gcloud auth login"
fi
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

say "プロジェクト: ${PROJECT_ID}（番号 ${PROJECT_NUMBER}）"
printf '対象リポジトリ: %s\n' "$REPO"
printf '\nこの内容で進めます。よければ Enter、やめるなら Ctrl-C。\n'
read -r _

say "1/5  必要なAPIを有効化"
gcloud services enable \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  drive.googleapis.com \
  --project="$PROJECT_ID"

say "2/5  サービスアカウントを用意"
if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "既にあります: ${SA_EMAIL}"
else
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="FREMING CURATED 納品" \
    --project="$PROJECT_ID"
fi

say "3/5  Workload Identity プールを用意"
if gcloud iam workload-identity-pools describe "$POOL" \
     --location=global --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "既にあります: ${POOL}"
else
  gcloud iam workload-identity-pools create "$POOL" \
    --location=global \
    --display-name="GitHub Actions" \
    --project="$PROJECT_ID"
fi

say "4/5  GitHub からの受け口を用意"
# attribute-condition でリポジトリを固定する。**これが無いと、GitHub の
# どのリポジトリからでもこのサービスアカウントを借りられる。**
if gcloud iam workload-identity-pools providers describe "$PROVIDER" \
     --location=global --workload-identity-pool="$POOL" \
     --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "既にあります: ${PROVIDER}"
else
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
    --location=global \
    --workload-identity-pool="$POOL" \
    --display-name="GitHub" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository == '${REPO}'" \
    --project="$PROJECT_ID"
fi

say "5/5  このリポジトリからだけ借りられるようにする"
MEMBER="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}"
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --role=roles/iam.workloadIdentityUser \
  --member="$MEMBER" \
  --project="$PROJECT_ID"

PROVIDER_PATH="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"

cat <<EOS

────────────────────────────────────────────────────────
Google Cloud 側は終わりました。残り2つ、手で作業があります。
────────────────────────────────────────────────────────

【1】共有ドライブにこのアカウントを追加する

    ${SA_EMAIL}

  Google Drive で納品先の共有ドライブを開き、メンバー管理から
  上のアドレスを「投稿者」以上で追加してください。

  **ここを飛ばすと、認証は通るのに書き込めません。** 一番よく詰まる所です。

【2】GitHub に2つ登録する

  https://github.com/${REPO}/settings/secrets/actions
  の「New repository secret」で2つ作ります。どちらも鍵ではなく
  ただの識別子なので、漏れても悪用できません。

  名前  GCP_WORKLOAD_IDENTITY_PROVIDER
  値    ${PROVIDER_PATH}

  名前  GCP_SERVICE_ACCOUNT
  値    ${SA_EMAIL}

────────────────────────────────────────────────────────
確認のしかた
────────────────────────────────────────────────────────

  https://github.com/${REPO}/actions/workflows/deliver.yml
  を開いて「Run workflow」を押してください。

  納品の前に Drive の疎通確認が入っています。権限が足りなければ、
  1件も中途半端にせずそこで止まります。

EOS
