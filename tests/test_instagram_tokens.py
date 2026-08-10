"""Instagram の長期トークン管理。

60日で失効し、60日間更新しなかったら復旧できない、という性質のものを
DBで持ち回す。更新の判断（24時間ルール・失効判定）を固定しておかないと、
気づいたときには再認可のやり直しになっている。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.instagram import tokens as ig

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    connection = connect(path)
    yield connection
    connection.close()


class StubHttp:
    """Graph API の代わり。何を呼ばれたかを記録する。"""

    def __init__(self, response: dict | None = None) -> None:
        self.response = response or {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, params: dict) -> dict:
        self.calls.append((url, params))
        return self.response


def test_round_trip(conn) -> None:
    ig.save_token(conn, "tok-1", now=NOW)
    record = ig.load_token(conn)
    assert record is not None
    assert record.value == "tok-1"
    # 新規保存は60日の仮置き。実際の残りは最初のリフレッシュで上書きされる
    assert record.days_left(NOW) == pytest.approx(60, abs=0.01)


def test_save_replaces_the_previous_token(conn) -> None:
    ig.save_token(conn, "tok-1", now=NOW)
    ig.save_token(conn, "tok-2", now=NOW)
    record = ig.load_token(conn)
    assert record.value == "tok-2"
    assert conn.execute("SELECT COUNT(*) AS n FROM api_tokens").fetchone()["n"] == 1


def test_refresh_without_a_token_does_nothing(conn) -> None:
    http = StubHttp()
    assert ig.refresh_token(conn, http, now=NOW) == "no_token"
    assert http.calls == []


def test_refresh_within_24_hours_is_skipped(conn) -> None:
    """取得直後は更新できない（Meta側の制約）。呼びに行かない。"""
    ig.save_token(conn, "tok-1", now=NOW - timedelta(hours=23))
    http = StubHttp()
    assert ig.refresh_token(conn, http, now=NOW) == "too_new"
    assert http.calls == []
    assert ig.load_token(conn).value == "tok-1"


def test_refresh_after_24_hours_rotates_the_token(conn) -> None:
    ig.save_token(conn, "tok-old", now=NOW - timedelta(days=2))
    http = StubHttp({"access_token": "tok-new", "token_type": "bearer",
                     "expires_in": 60 * 86400})
    assert ig.refresh_token(conn, http, now=NOW) == "refreshed"

    (url, params), = http.calls
    assert url.endswith("/refresh_access_token")
    assert params["grant_type"] == "ig_refresh_token"
    assert params["access_token"] == "tok-old"

    record = ig.load_token(conn)
    assert record.value == "tok-new"
    assert record.days_left(NOW) == pytest.approx(60, abs=0.01)


def test_an_expired_token_is_reported_not_refreshed(conn) -> None:
    """60日を過ぎたトークンは更新で戻せない。無駄に呼ばず、再認可を促す。"""
    ig.save_token(conn, "tok-dead", lifetime_sec=86400, now=NOW - timedelta(days=2))
    http = StubHttp()
    assert ig.refresh_token(conn, http, now=NOW) == "expired"
    assert http.calls == []


def test_a_response_without_a_token_raises(conn) -> None:
    """空の応答で古いトークンを上書きしない。"""
    ig.save_token(conn, "tok-old", now=NOW - timedelta(days=2))
    http = StubHttp({"token_type": "bearer"})
    with pytest.raises(ig.InstagramError, match="access_token"):
        ig.refresh_token(conn, http, now=NOW)
    assert ig.load_token(conn).value == "tok-old"


def test_fetch_profile_asks_for_username() -> None:
    http = StubHttp({"user_id": "123", "username": "frmg.jpn"})
    profile = ig.fetch_profile("tok", http)
    assert profile["username"] == "frmg.jpn"
    (url, params), = http.calls
    assert url.endswith("/me")
    assert params["access_token"] == "tok"


# ----------------------------------------------------------------------
# 認可コードの引き換え。
#
# ダッシュボードの「Generate token」は沢田の画面で管理者にログインして
# もらう形になる。それを避け、管理者が自分の端末で完結できるようにした
# 経路。ネットワークは呼ばず、Meta の応答を差し替えて検証する。


def test_authorization_url_has_everything_meta_requires() -> None:
    url = ig.authorization_url("123", "https://example.com/ig/callback")
    assert url.startswith("https://www.instagram.com/oauth/authorize?")
    assert "client_id=123" in url
    assert "response_type=code" in url
    # リダイレクトURIとスコープはURLエンコードされて入る
    assert "https%3A%2F%2Fexample.com%2Fig%2Fcallback" in url
    assert "instagram_business_basic" in url
    assert "instagram_business_content_publish" in url
    # リーチが読めないと週次リールの7枚を選べない。**これが欠けたURLで
    # 認可してもらうと、権限が付かないまま作業だけが終わる。**
    assert "instagram_business_manage_insights" in url


def test_authorization_url_carries_no_secret() -> None:
    """管理者に送るURLなので、app secret が混ざってはいけない。"""
    url = ig.authorization_url("123", "https://example.com/ig/callback")
    assert "secret" not in url.lower()


def test_exchange_code_returns_the_long_lived_token() -> None:
    posted, got = {}, {}

    def fake_post(url, data):
        posted.update(data)
        return {"access_token": "SHORT", "user_id": 1}

    def fake_get(url, params):
        got.update(params)
        return {"access_token": "LONG", "expires_in": 5184000}

    value = ig.exchange_code(
        "CODE", "123", "SECRET", "https://example.com/ig/callback",
        http_post=fake_post, http_get=fake_get,
    )
    assert value == "LONG"
    # 短期→長期の2段。片方だけでは投稿を続けられない。
    assert posted["grant_type"] == "authorization_code"
    assert posted["code"] == "CODE"
    assert posted["redirect_uri"] == "https://example.com/ig/callback"
    assert got["grant_type"] == "ig_exchange_token"
    assert got["access_token"] == "SHORT"


def test_exchange_code_fails_loudly_when_the_code_is_spent() -> None:
    """code は一度きり。黙って空を保存しない。"""
    def fake_post(url, data):
        return {"error_type": "OAuthException", "code": 400}

    with pytest.raises(ig.InstagramError):
        ig.exchange_code(
            "USED", "123", "SECRET", "https://example.com/ig/callback",
            http_post=fake_post, http_get=lambda *a, **k: {},
        )


def test_exchange_code_fails_when_the_long_lived_step_returns_nothing() -> None:
    with pytest.raises(ig.InstagramError):
        ig.exchange_code(
            "CODE", "123", "SECRET", "https://example.com/ig/callback",
            http_post=lambda url, data: {"access_token": "SHORT"},
            http_get=lambda url, params: {},
        )


# ----------------------------------------------------------------------
# 受け取った文字列の正規化。
#
# Meta は「期限切れ」も「余計な文字が混ざっている」も同じ
# `Invalid authorization code` を返す。入力側で潰しておかないと
# 切り分けができない。


def test_clean_code_strips_the_fragment_meta_appends() -> None:
    """Meta はリダイレクトURLの code に `#_` を付ける。仕様として明記がある。"""
    assert ig.clean_code("AQK4wiCO#_") == "AQK4wiCO"


def test_clean_code_takes_the_code_out_of_a_pasted_url() -> None:
    """「画面のURLを送ってください」と受け取られた場合。"""
    url = "https://freming-curated-review.onrender.com/ig/callback?code=AQK4wiCO#_"
    assert ig.clean_code(url) == "AQK4wiCO"


def test_clean_code_trims_whitespace_from_chat_wrapping() -> None:
    assert ig.clean_code("  AQK4wiCO \n") == "AQK4wiCO"


def test_clean_code_leaves_a_clean_code_alone() -> None:
    assert ig.clean_code("AQK4wiCO") == "AQK4wiCO"


def test_clean_code_on_empty_input() -> None:
    assert ig.clean_code("   ") == ""
    assert ig.clean_code(None) == ""


def test_exchange_code_normalises_before_sending() -> None:
    """正規化を通さずに送っていないこと。"""
    sent = {}

    def fake_post(url, data):
        sent.update(data)
        return {"access_token": "SHORT"}

    ig.exchange_code(
        "AQK4wiCO#_", "123", "SECRET", "https://example.com/ig/callback",
        http_post=fake_post, http_get=lambda url, params: {"access_token": "LONG"},
    )
    assert sent["code"] == "AQK4wiCO"


def test_exchange_code_refuses_an_empty_code() -> None:
    with pytest.raises(ig.InstagramError):
        ig.exchange_code(
            "  ", "123", "SECRET", "https://example.com/ig/callback",
            http_post=lambda url, data: {}, http_get=lambda url, params: {},
        )
