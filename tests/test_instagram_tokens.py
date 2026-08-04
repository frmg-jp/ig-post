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
