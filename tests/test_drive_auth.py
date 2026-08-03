"""Drive の認証モード切替まわりの検証。"""

from __future__ import annotations

import json

import pytest
from googleapiclient.errors import HttpError
from pydantic import ValidationError

from freming.config import DriveConfig
from freming.delivery.drive import (
    DriveApiDisabledError,
    DriveAuthError,
    DriveClient,
    DriveError,
    DrivePermissionError,
    DriveQuotaError,
    _browser_available,
    _classify,
)


def _cfg(**kwargs) -> DriveConfig:
    base = {"parent_folder_id": "folder123"}
    base.update(kwargs)
    return DriveConfig.model_validate(base)


def test_default_auth_mode_is_oauth() -> None:
    assert _cfg().auth_mode == "oauth"


def test_unknown_auth_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _cfg(auth_mode="password")


def test_placeholder_folder_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="実際のフォルダID"):
        DriveConfig.model_validate({"parent_folder_id": "PUT_IG_FRMG_PHOTO_FOLDER_ID_HERE"})


def test_missing_service_account_key_explains_the_alternative(tmp_path) -> None:
    cfg = _cfg(auth_mode="service_account", credentials_path=tmp_path / "nope.json")
    with pytest.raises(DriveAuthError) as excinfo:
        DriveClient(cfg)
    message = str(excinfo.value)
    assert "組織ポリシー" in message
    assert "oauth" in message


def test_missing_oauth_client_secret_explains_how_to_create_it(tmp_path) -> None:
    cfg = _cfg(
        auth_mode="oauth",
        oauth_client_secret_path=tmp_path / "nope.json",
        oauth_token_path=tmp_path / "token.json",
    )
    with pytest.raises(DriveAuthError, match="デスクトップアプリ"):
        DriveClient(cfg)


def test_broken_token_falls_back_to_client_secret_error(tmp_path) -> None:
    """壊れたトークンがあっても黙って失敗せず、再認証の案内に落ちること。"""
    token = tmp_path / "token.json"
    token.write_text("{ not json", encoding="utf-8")
    cfg = _cfg(
        auth_mode="oauth",
        oauth_client_secret_path=tmp_path / "nope.json",
        oauth_token_path=token,
    )
    with pytest.raises(DriveAuthError, match="OAuthクライアントシークレットが見つかりません"):
        DriveClient(cfg)


def test_background_delivery_never_opens_the_consent_screen(tmp_path) -> None:
    """自動納品から呼ばれたときは同意画面に進まないこと。

    人が画面の前にいるとは限らない経路なので、ブラウザを開いて
    応答を待ち続けると納品スレッドがそのまま止まる。
    """
    secret = tmp_path / "client.json"
    secret.write_text("{}", encoding="utf-8")   # 存在はするが、そこまで進まない
    cfg = _cfg(
        auth_mode="oauth",
        oauth_client_secret_path=secret,
        oauth_token_path=tmp_path / "token.json",
    )
    with pytest.raises(DriveAuthError, match="check-drive"):
        DriveClient(cfg, allow_interactive=False)


class _FakeResponse:
    """HttpError に渡す最小限のレスポンス。"""

    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"


def _http_error(status: int, reason: str, message: str = "") -> HttpError:
    body = json.dumps(
        {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}
    ).encode("utf-8")
    return HttpError(_FakeResponse(status), body)


def test_api_not_enabled_is_distinguished_from_permission_error() -> None:
    """403 でも accessNotConfigured は権限不足ではなく API 未有効化として扱う。"""
    err = _classify(
        _http_error(403, "accessNotConfigured", "Drive API has not been used in project 123"),
        "files.get",
    )
    assert isinstance(err, DriveApiDisabledError)
    assert not isinstance(err, DrivePermissionError)
    assert "drive.googleapis.com" in str(err)
    assert "project 123" in str(err)


def test_storage_quota_error_points_to_shared_drive() -> None:
    err = _classify(_http_error(403, "storageQuotaExceeded"), "アップロード")
    assert isinstance(err, DriveQuotaError)
    assert "共有ドライブ" in str(err)


def test_plain_403_is_a_permission_error() -> None:
    err = _classify(_http_error(403, "insufficientFilePermissions"), "files.get")
    assert isinstance(err, DrivePermissionError)


def test_404_reports_missing_target() -> None:
    err = _classify(_http_error(404, "notFound"), "files.get")
    assert isinstance(err, DriveError)
    assert "見つかりません" in str(err)


def test_browser_detection_reports_false_over_ssh(monkeypatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 22")
    assert _browser_available() is False
