"""審査UIのアクセス制限。

審査UIは承認・非承認をその場で確定させる。外に出した状態で認証が
抜けていると、URLを知っている人が納品の引き金を引けてしまう。
「認証なしで外向けに立ち上がらない」ことを固定しておく。
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from freming.config import load_config
from freming.db.migrate import migrate
from freming.web.app import create_app
from freming.web.auth import BasicAuth, credentials_from_env, require_credentials


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """実行環境に値が残っていても、テストの前提を揺らさない。"""
    monkeypatch.delenv("REVIEW_UI_USER", raising=False)
    monkeypatch.delenv("REVIEW_UI_PASSWORD", raising=False)


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "review.db"
    migrate(cfg.app.db_path)
    return cfg


def _header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --- 環境変数の読み取り ---------------------------------------------------


def test_no_credentials_means_no_auth(monkeypatch) -> None:
    """手元での利用は今まで通り。設定しなければ認証はかからない。"""
    assert credentials_from_env() is None


def test_a_half_filled_pair_is_an_error(monkeypatch) -> None:
    """片方だけの設定を「認証なし」に倒さない。設定漏れを黙って通すと危ない。"""
    monkeypatch.setenv("REVIEW_UI_USER", "frmg")
    with pytest.raises(RuntimeError, match="両方"):
        credentials_from_env()


# --- 待ち受けアドレスとの整合 ---------------------------------------------


def test_loopback_does_not_require_credentials() -> None:
    assert require_credentials("127.0.0.1") is None


def test_a_public_host_without_credentials_is_refused() -> None:
    """0.0.0.0 で待ち受けるなら資格情報が要る。"""
    with pytest.raises(RuntimeError, match="REVIEW_UI_USER"):
        require_credentials("0.0.0.0")


def test_a_public_host_with_credentials_is_allowed(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_UI_USER", "frmg")
    monkeypatch.setenv("REVIEW_UI_PASSWORD", "pw")
    assert require_credentials("0.0.0.0") == BasicAuth("frmg", "pw")


# --- 実際のリクエスト -----------------------------------------------------


@pytest.fixture()
def guarded(config):
    return TestClient(create_app(config, auth=BasicAuth("frmg", "s3cret")))


def test_the_list_is_closed_without_credentials(guarded) -> None:
    response = guarded.get("/")
    assert response.status_code == 401
    assert "Basic" in response.headers["WWW-Authenticate"]


def test_the_wrong_password_is_refused(guarded) -> None:
    assert guarded.get("/", headers=_header("frmg", "wrong")).status_code == 401


def test_the_wrong_user_is_refused(guarded) -> None:
    assert guarded.get("/", headers=_header("other", "s3cret")).status_code == 401


def test_a_broken_header_is_refused(guarded) -> None:
    """base64 として壊れた値でも 500 にせず 401 で返す。"""
    assert guarded.get("/", headers={"Authorization": "Basic !!!!"}).status_code == 401
    assert guarded.get("/", headers={"Authorization": "Bearer token"}).status_code == 401


def test_the_right_credentials_get_in(guarded) -> None:
    assert guarded.get("/", headers=_header("frmg", "s3cret")).status_code == 200


def test_approving_is_closed_too(guarded) -> None:
    """一覧だけでなく、状態を変える経路も塞がっていること。"""
    assert guarded.post("/approve/1").status_code == 401


def test_health_check_stays_open(guarded) -> None:
    """ホスティング側の死活監視は資格情報を送らない。中身は返さない。"""
    response = guarded.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ----------------------------------------------------------------------
# Instagram 認可の着地先。
#
# @frmg.jpn の管理者は審査UIの資格情報を持っていないので、この経路だけは
# 認証を通さない。代わりに、物件のデータも内部の導線も出さない。


def test_ig_callback_is_reachable_without_credentials() -> None:
    client = TestClient(create_app(load_config("config.yaml"), auth=BasicAuth("u", "p")))
    assert client.get("/ig/callback?code=ABC123").status_code == 200


def test_ig_callback_shows_the_code_to_copy() -> None:
    client = TestClient(create_app(load_config("config.yaml"), auth=BasicAuth("u", "p")))
    body = client.get("/ig/callback?code=ABC123").text
    assert "ABC123" in body


def test_ig_callback_shows_the_error_when_denied() -> None:
    client = TestClient(create_app(load_config("config.yaml"), auth=BasicAuth("u", "p")))
    body = client.get("/ig/callback?error_description=User+denied+access").text
    assert "User denied access" in body


def test_ig_callback_does_not_leak_the_review_ui() -> None:
    """未認証の相手に見せる画面なので、審査UIの導線を出さない。"""
    client = TestClient(create_app(load_config("config.yaml"), auth=BasicAuth("u", "p")))
    body = client.get("/ig/callback?code=ABC123").text
    assert "ルール候補" not in body
    assert 'href="/rules"' not in body
    assert "承認" not in body


def test_everything_else_still_needs_credentials() -> None:
    """歯止めが緩んでいないこと。"""
    client = TestClient(create_app(load_config("config.yaml"), auth=BasicAuth("u", "p")))
    for path in ("/", "/rules", "/ig", "/ig/callback/../"):
        assert client.get(path, follow_redirects=False).status_code == 401, path


# --- [9] 投稿する画像の配り先 --------------------------------------------
#
# **Meta がここへ取りに来る。** 相手に資格情報を渡す方法がないので、
# ここだけは認証を通さない。代わりに token を推測できない文字列にしてある。
# 「うっかり認証がかかって投稿が全部失敗する」ことも、「間に合わせで
# 全経路の認証を外す」ことも防ぎたいので、両方向を固定しておく。


def test_投稿画像は認証なしで取れる(config) -> None:
    from freming.db.connection import connect
    from freming.db.repository import create_post
    from freming.instagram.media import store_media

    conn = connect(config.app.target())
    post_id = create_post(conn, "feed", "2026-08-10T01:00:00+00:00")
    token = store_media(conn, post_id, b"\xff\xd8jpeg")
    conn.close()

    client = TestClient(create_app(config, auth=BasicAuth("u", "p")))
    response = client.get(f"/m/{token}")
    assert response.status_code == 200
    assert response.content == b"\xff\xd8jpeg"


def test_知らない画像は404で認証は要求しない(config) -> None:
    client = TestClient(create_app(config, auth=BasicAuth("u", "p")))
    assert client.get("/m/ないよ").status_code == 404


def test_予定表は認証が要る(config) -> None:
    """配るのは画像だけ。予定表まで外に出さない。"""
    client = TestClient(create_app(config, auth=BasicAuth("u", "p")))
    assert client.get("/schedule").status_code == 401
    assert client.get("/schedule", headers=_header("u", "p")).status_code == 200


def test_投稿の操作は認証が要る(config) -> None:
    client = TestClient(create_app(config, auth=BasicAuth("u", "p")))
    assert client.post("/posts/1/skip").status_code == 401
