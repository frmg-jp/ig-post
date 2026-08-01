"""Drive の認証モード切替まわりの検証。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from freming.config import DriveConfig
from freming.delivery.drive import DriveAuthError, DriveClient, _browser_available


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


def test_browser_detection_reports_false_over_ssh(monkeypatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 22")
    assert _browser_available() is False
