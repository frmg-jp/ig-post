"""Google Drive クライアント（サービスアカウント認証）。

方針:
- 全リクエストで supportsAllDrives=True を指定し、共有ドライブでも動作させる。
- 例外は握りつぶさない。原因が特定できるものは専用の例外型に変換して投げ、
  それ以外はスタックトレース付きでログに残したうえで再送出する。
- アップロード後は必ず files.get でサイズを再取得し、0 バイトでないことを確認する。
"""

from __future__ import annotations

import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import google.auth
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaInMemoryUpload

from freming.config import DriveConfig
from freming.logging_setup import get_logger

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DEFAULT_OAUTH_PORT = 8765
T = TypeVar("T")


class DriveError(RuntimeError):
    """Drive 操作の失敗（基底）。"""


class DriveAuthError(DriveError):
    """認証情報が読めない・不正。"""


class DrivePermissionError(DriveError):
    """対象フォルダへの権限不足。共有漏れが典型。"""


class DriveApiDisabledError(DriveError):
    """Cloud プロジェクトで Drive API が有効化されていない（accessNotConfigured）。

    権限の問題と同じ 403 で返るが、対処はまったく別物なので分けて扱う。
    """


class DriveQuotaError(DriveError):
    """サービスアカウントの保存容量不足。

    サービスアカウントはマイドライブに保存容量を持たないため、個人のマイドライブ
    配下へファイルを作成しようとするとこのエラーになる。フォルダ（0バイト）は
    作成できてしまうため「フォルダはあるのに画像が入らない」症状として現れる。
    解決策は共有ドライブ（Shared Drive）配下へ納品すること。
    """


class DriveUploadVerificationError(DriveError):
    """アップロードは成功扱いだが、実体が 0 バイトなど検証に失敗した。"""


@dataclass(frozen=True)
class UploadedFile:
    id: str
    name: str
    size: int
    web_view_link: str | None = None


def _classify(exc: HttpError, context: str) -> DriveError:
    """HttpError を原因が分かる例外型に変換する。"""
    status = getattr(exc.resp, "status", None)
    reason = ""
    message = ""
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        error = payload.get("error", {})
        errors = error.get("errors", [])
        reason = errors[0].get("reason", "") if errors else error.get("status", "")
        message = error.get("message", "")
    except Exception:  # noqa: BLE001 - エラー本文が読めなくても分類は続行
        reason = ""

    if reason == "accessNotConfigured":
        detail = f"\n  APIからの応答: {message}" if message else ""
        return DriveApiDisabledError(
            f"{context}: この Cloud プロジェクトで Google Drive API が有効になっていません "
            f"({reason})。\n"
            "  https://console.cloud.google.com/apis/library/drive.googleapis.com を開き、"
            "OAuthクライアントを作成したプロジェクトを選んで「有効にする」を押してください。"
            f"{detail}"
        )

    if reason in {"storageQuotaExceeded", "quotaExceeded"} and status == 403:
        return DriveQuotaError(
            f"{context}: 保存容量エラー ({reason})。"
            "サービスアカウントで認証している場合、マイドライブには保存容量がありません。"
            "納品先を共有ドライブ（Shared Drive）に変更してください。"
        )
    if status in (401, 403):
        return DrivePermissionError(
            f"{context}: 権限エラー (status={status}, reason={reason or 'unknown'})。"
            "認証したアカウントが対象フォルダへの編集権限を持っているか確認してください。"
        )
    if status == 404:
        return DriveError(f"{context}: 対象が見つかりません (404)。フォルダIDを確認してください。")
    return DriveError(f"{context}: Drive API エラー (status={status}, reason={reason})")


class DriveClient:
    """Drive v3 の薄いラッパ。"""

    def __init__(
        self,
        config: DriveConfig,
        open_browser: bool = True,
        allow_interactive: bool = True,
    ) -> None:
        self.config = config
        self._credentials = self._load_credentials(
            config, open_browser=open_browser, allow_interactive=allow_interactive
        )
        self.service = build("drive", "v3", credentials=self._credentials, cache_discovery=False)

    # ------------------------------------------------------------------
    # 初期化
    # ------------------------------------------------------------------
    @classmethod
    def _load_credentials(
        cls,
        config: DriveConfig,
        open_browser: bool = True,
        allow_interactive: bool = True,
    ) -> Credentials:
        mode = config.auth_mode
        log.info("Drive 認証モード: %s", mode)
        if mode == "service_account":
            return cls._load_service_account(config.credentials_path)
        if mode == "oauth":
            return cls._load_oauth(
                config.oauth_client_secret_path,
                config.oauth_token_path,
                open_browser,
                allow_interactive,
            )
        return cls._load_adc()

    @staticmethod
    def _load_service_account(path: Path) -> Credentials:
        if not path.exists():
            raise DriveAuthError(
                f"サービスアカウント鍵が見つかりません: {path.resolve()}\n"
                "組織ポリシーで鍵を作成できない場合は、config.yaml の "
                "drive.auth_mode を oauth または adc にしてください。"
            )
        try:
            return service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)
        except Exception as exc:  # noqa: BLE001
            log.exception("サービスアカウント鍵の読み込みに失敗")
            raise DriveAuthError(f"サービスアカウント鍵が不正です: {path} ({exc})") from exc

    @staticmethod
    def _load_oauth(
        client_secret_path: Path,
        token_path: Path,
        open_browser: bool = True,
        allow_interactive: bool = True,
    ) -> Credentials:
        """OAuthクライアントで人のアカウントとして認証する。

        初回のみブラウザで同意画面が開き、以降は token_path のリフレッシュ
        トークンを使うため対話は発生しない。鍵ファイルを扱わないため、
        サービスアカウント鍵の作成を禁じる組織ポリシーの影響を受けない。

        allow_interactive=False では同意画面に進まず DriveAuthError にする。
        自動納品のような、人が画面の前にいない経路から呼ばれたときに、
        ブラウザを開いて応答を待ち続けないようにするため。
        """
        creds: UserCredentials | None = None
        if token_path.exists():
            try:
                creds = UserCredentials.from_authorized_user_file(str(token_path), SCOPES)
            except Exception:  # noqa: BLE001 - 壊れていれば取り直す
                log.warning("保存済みトークンを読めませんでした。再認証します: %s", token_path)
                creds = None

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(AuthRequest())
                _save_token(creds, token_path)
                return creds
            except Exception:  # noqa: BLE001 - リフレッシュ失敗時は再認証に落とす
                log.warning("トークンの更新に失敗しました。再認証します。", exc_info=True)

        if not allow_interactive:
            raise DriveAuthError(
                "Drive の認証が切れています（対話的な再認証が必要）。\n"
                "  python -m freming.cli check-drive\n"
                "を1回実行して認証し直してください。"
            )

        if not client_secret_path.exists():
            raise DriveAuthError(
                f"OAuthクライアントシークレットが見つかりません: {client_secret_path.resolve()}\n"
                "Google Cloud の「認証情報」→「OAuth クライアント ID」→ 種類「デスクトップアプリ」\n"
                "で作成し、ダウンロードしたJSONをこのパスに置いてください。"
            )

        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:  # pragma: no cover
            raise DriveAuthError(
                "google-auth-oauthlib が未インストールです: pip install -e ."
            ) from exc

        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)

        if open_browser and not _browser_available():
            log.warning("この環境では既定のブラウザを開けません。URLを表示します。")
            open_browser = False

        success_message = "認証が完了しました。このタブを閉じてターミナルに戻ってください。"

        # 既定はポート固定（リモート実行時のSSHポート転送を案内できる）。
        # 使用中なら空きポートに自動で切り替える。
        try:
            log.info("認証待ち受けポート: %d", DEFAULT_OAUTH_PORT)
            creds = flow.run_local_server(
                port=DEFAULT_OAUTH_PORT,
                open_browser=open_browser,
                authorization_prompt_message=_auth_prompt(open_browser, DEFAULT_OAUTH_PORT),
                success_message=success_message,
            )
        except OSError as exc:
            log.warning(
                "ポート %d は使用中のため空きポートで再試行します（%s）。"
                "前回の認証プロセスが残っている可能性があります。",
                DEFAULT_OAUTH_PORT, exc,
            )
            try:
                creds = flow.run_local_server(
                    port=0,
                    open_browser=open_browser,
                    authorization_prompt_message=_auth_prompt(open_browser, None),
                    success_message=success_message,
                )
            except OSError as exc2:
                raise DriveAuthError(
                    f"認証用のローカルサーバーを起動できませんでした（{exc2}）。"
                ) from exc2
        except Exception as exc:  # noqa: BLE001 - 同意画面での拒否などを分かる形にする
            log.exception("OAuth 認証に失敗")
            if "access_denied" in str(exc):
                raise DriveAuthError(
                    "同意画面でアクセスが拒否されました（access_denied）。\n"
                    "  ・OAuth同意画面が「テスト中」の場合、ログインしたアカウントを\n"
                    "    「テストユーザー」に追加する必要があります。\n"
                    "  ・User Type が「内部」の場合、組織外のアカウント（@gmail.com など）は\n"
                    "    使用できません。組織のアカウントでログインしてください。\n"
                    "  ・納品先フォルダを操作できるアカウントでログインしているかも確認してください。"
                ) from exc
            raise DriveAuthError(f"OAuth 認証に失敗しました: {exc}") from exc

        _save_token(creds, token_path)
        return creds

    @staticmethod
    def _load_adc() -> Credentials:
        """Application Default Credentials。

        gcloud auth application-default login / Workload Identity 連携 /
        サービスアカウントの権限借用（--impersonate-service-account）に対応する。
        """
        try:
            creds, _project = google.auth.default(scopes=SCOPES)
            return creds
        except Exception as exc:  # noqa: BLE001
            log.exception("ADC の取得に失敗")
            raise DriveAuthError(
                "Application Default Credentials を取得できませんでした。\n"
                "  gcloud auth application-default login "
                '--scopes="https://www.googleapis.com/auth/drive,'
                'https://www.googleapis.com/auth/cloud-platform"\n'
                f"を実行してください。({exc})"
            ) from exc

    @property
    def account_hint(self) -> str:
        """認証に使ったアカウント（判明する範囲で）。"""
        email = getattr(self._credentials, "service_account_email", None)
        if email:
            return email
        return f"({self.config.auth_mode} で認証)"

    # ------------------------------------------------------------------
    # 内部: リトライ
    # ------------------------------------------------------------------
    def _execute(self, request: Any, context: str) -> Any:
        attempts = self.config.retry.max_attempts
        backoff = self.config.retry.backoff_factor
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return request.execute()
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                if status in _RETRYABLE_STATUS and attempt < attempts:
                    wait = (backoff ** (attempt - 1)) + random.uniform(0, 0.5)
                    log.warning(
                        "%s: 一時エラー status=%s、%.1f秒後に再試行 (%d/%d)",
                        context, status, wait, attempt, attempts,
                    )
                    time.sleep(wait)
                    last_exc = exc
                    continue
                log.exception("%s に失敗", context)
                raise _classify(exc, context) from exc
            except Exception:
                log.exception("%s に失敗（想定外）", context)
                raise
        raise DriveError(f"{context}: 再試行上限に到達") from last_exc

    def _drive_kwargs(self) -> dict[str, Any]:
        return {"supportsAllDrives": True}

    # ------------------------------------------------------------------
    # 参照系
    # ------------------------------------------------------------------
    def get_file(self, file_id: str, fields: str = "id,name,mimeType,size,parents") -> dict:
        return self._execute(
            self.service.files().get(fileId=file_id, fields=fields, **self._drive_kwargs()),
            f"files.get({file_id})",
        )

    def get_folder_info(self, folder_id: str) -> dict:
        """納品先フォルダの素性（共有ドライブか、書き込めるか）を取得する。"""
        fields = (
            "id,name,mimeType,driveId,parents,owners(emailAddress),"
            "capabilities(canAddChildren,canEdit),createdTime,modifiedTime"
        )
        return self.get_file(folder_id, fields=fields)

    def list_children(self, folder_id: str, page_size: int = 100) -> list[dict]:
        params: dict[str, Any] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "files(id,name,mimeType,size,createdTime,modifiedTime)",
            "pageSize": page_size,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if self.config.shared_drive_id:
            params["corpora"] = "drive"
            params["driveId"] = self.config.shared_drive_id
        result = self._execute(self.service.files().list(**params), f"files.list({folder_id})")
        return result.get("files", [])

    def storage_quota(self) -> dict:
        """サービスアカウント自身の保存容量情報。"""
        return self._execute(
            self.service.about().get(fields="user(emailAddress),storageQuota"),
            "about.get",
        )

    # ------------------------------------------------------------------
    # 更新系
    # ------------------------------------------------------------------
    def create_folder(self, name: str, parent_id: str) -> str:
        metadata = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        created = self._execute(
            self.service.files().create(body=metadata, fields="id", **self._drive_kwargs()),
            f"フォルダ作成({name})",
        )
        log.info("フォルダを作成: %s (id=%s)", name, created["id"])
        return created["id"]

    def find_folder(self, name: str, parent_id: str) -> str | None:
        escaped = name.replace("'", r"\'")
        params: dict[str, Any] = {
            "q": (
                f"name = '{escaped}' and '{parent_id}' in parents "
                f"and mimeType = '{FOLDER_MIME}' and trashed = false"
            ),
            "fields": "files(id,name)",
            "pageSize": 1,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if self.config.shared_drive_id:
            params["corpora"] = "drive"
            params["driveId"] = self.config.shared_drive_id
        files = self._execute(
            self.service.files().list(**params), f"フォルダ検索({name})"
        ).get("files", [])
        return files[0]["id"] if files else None

    def upload_file(
        self,
        local_path: str | Path,
        name: str,
        parent_id: str,
        mime_type: str = "application/octet-stream",
    ) -> UploadedFile:
        local_path = Path(local_path)
        if not local_path.exists():
            raise DriveError(f"アップロード対象が存在しません: {local_path}")
        media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=False)
        return self._create_and_verify(
            body={"name": name, "parents": [parent_id]}, media=media, name=name
        )

    def upload_bytes(
        self,
        data: bytes,
        name: str,
        parent_id: str,
        mime_type: str = "text/plain",
    ) -> UploadedFile:
        media = MediaInMemoryUpload(data, mimetype=mime_type, resumable=False)
        return self._create_and_verify(
            body={"name": name, "parents": [parent_id]}, media=media, name=name
        )

    def _create_and_verify(self, body: dict, media: Any, name: str) -> UploadedFile:
        created = self._execute(
            self.service.files().create(
                body=body, media_body=media, fields="id,name", **self._drive_kwargs()
            ),
            f"アップロード({name})",
        )
        file_id = created["id"]

        if not self.config.verify_uploaded_size:
            return UploadedFile(id=file_id, name=created.get("name", name), size=-1)

        # 「作成には成功したが中身が空」を検出するため、必ず取り直す
        meta = self.get_file(file_id, fields="id,name,size,webViewLink")
        size = int(meta.get("size", 0) or 0)
        if size <= 0:
            raise DriveUploadVerificationError(
                f"アップロード後の検証に失敗: {name} (id={file_id}) のサイズが {size} バイトです。"
            )
        log.info("アップロード完了: %s (id=%s, %d bytes)", name, file_id, size)
        return UploadedFile(
            id=file_id,
            name=meta.get("name", name),
            size=size,
            web_view_link=meta.get("webViewLink"),
        )

    def delete(self, file_id: str) -> None:
        self._execute(
            self.service.files().delete(fileId=file_id, **self._drive_kwargs()),
            f"削除({file_id})",
        )
        log.info("削除しました: id=%s", file_id)


def _auth_prompt(open_browser: bool, port: int | None) -> str:
    """同意画面のURLを目立つ形で表示するためのメッセージ。"""
    if open_browser:
        tail = "  ブラウザが自動で開かない場合は、上のURLをコピーして開いてください。\n"
    elif port is not None:
        tail = (
            "  上のURLを、このマシンのブラウザで開いてください。\n"
            "  リモートサーバーで実行している場合は、手元のPCから\n"
            f"    ssh -L {port}:localhost:{port} <ユーザ>@<サーバ>\n"
            "  のようにポート転送してから開いてください。\n"
        )
    else:
        tail = (
            "  上のURLを、このマシンのブラウザで開いてください。\n"
            "  リモート実行の場合は、URL内の redirect_uri のポート番号を\n"
            "  手元のPCへSSHポート転送してから開いてください。\n"
        )
    return (
        "\n"
        + "=" * 68
        + "\n Google の同意画面を開いてください（初回のみ）\n"
        + "=" * 68
        + "\n\n    {url}\n\n"
        + tail
        + "=" * 68
        + "\n"
    )


def _browser_available() -> bool:
    """既定のブラウザを起動できる環境か判定する。

    SSH接続先やコンテナなど GUI のない環境では False。
    """
    import os
    import webbrowser

    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    try:
        webbrowser.get()
        return True
    except webbrowser.Error:
        return False


def _save_token(creds: UserCredentials, token_path: Path) -> None:
    """リフレッシュトークンを保存する。鍵と同等の秘密なので権限を絞る。"""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:  # pragma: no cover - Windows など
        log.debug("トークンファイルの権限設定をスキップしました: %s", token_path)
    log.info("認証トークンを保存しました: %s", token_path)


def build_client(config: DriveConfig, *, allow_interactive: bool = True) -> DriveClient:
    return DriveClient(config, allow_interactive=allow_interactive)
