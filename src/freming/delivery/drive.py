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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaInMemoryUpload

from freming.config import DriveConfig
from freming.logging_setup import get_logger

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
T = TypeVar("T")


class DriveError(RuntimeError):
    """Drive 操作の失敗（基底）。"""


class DriveAuthError(DriveError):
    """認証情報が読めない・不正。"""


class DrivePermissionError(DriveError):
    """対象フォルダへの権限不足。サービスアカウントへの共有漏れが典型。"""


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
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        errors = payload.get("error", {}).get("errors", [])
        reason = errors[0].get("reason", "") if errors else payload.get("error", {}).get(
            "status", ""
        )
    except Exception:  # noqa: BLE001 - エラー本文が読めなくても分類は続行
        reason = ""

    if reason in {"storageQuotaExceeded", "quotaExceeded"} and status == 403:
        return DriveQuotaError(
            f"{context}: 保存容量エラー ({reason})。"
            "サービスアカウントはマイドライブに保存容量を持ちません。"
            "納品先を共有ドライブ（Shared Drive）に変更してください。"
        )
    if status in (401, 403):
        return DrivePermissionError(
            f"{context}: 権限エラー (status={status}, reason={reason or 'unknown'})。"
            "対象フォルダをサービスアカウントのメールアドレスに「編集者」として"
            "共有しているか確認してください。"
        )
    if status == 404:
        return DriveError(f"{context}: 対象が見つかりません (404)。フォルダIDを確認してください。")
    return DriveError(f"{context}: Drive API エラー (status={status}, reason={reason})")


class DriveClient:
    """Drive v3 の薄いラッパ。"""

    def __init__(self, config: DriveConfig) -> None:
        self.config = config
        self._credentials = self._load_credentials(config.credentials_path)
        self.service = build("drive", "v3", credentials=self._credentials, cache_discovery=False)

    # ------------------------------------------------------------------
    # 初期化
    # ------------------------------------------------------------------
    @staticmethod
    def _load_credentials(path: Path) -> service_account.Credentials:
        if not path.exists():
            raise DriveAuthError(
                f"サービスアカウント鍵が見つかりません: {path.resolve()}\n"
                "Google Cloud でサービスアカウントを作成し、JSON鍵をこのパスに置いてください。"
            )
        try:
            return service_account.Credentials.from_service_account_file(
                str(path), scopes=SCOPES
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("サービスアカウント鍵の読み込みに失敗")
            raise DriveAuthError(f"サービスアカウント鍵が不正です: {path} ({exc})") from exc

    @property
    def service_account_email(self) -> str:
        return getattr(self._credentials, "service_account_email", "(不明)")

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


def build_client(config: DriveConfig) -> DriveClient:
    return DriveClient(config)
