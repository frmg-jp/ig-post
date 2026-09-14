"""公開済みの投稿が**実際に残っているか**の確認。

2026-09 に、published と記録されているのにアカウントから消えている投稿が
3件見つかった（09/05・09/07・09/12）。3件とも media_id と permalink が
発行済みで、こちらの投稿処理は最後まで成功していた。

**気づいたのは週次リールの件数が合わなかったとき**で、最初の1件から
1週間たっていた。件数を数えているだけでは気づけない。
"""

from __future__ import annotations

import pytest

from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import published_with_media
from freming.instagram.publish import media_exists
from freming.instagram.tokens import InstagramError


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    conn = connect(path)
    yield conn
    conn.close()


def _add(conn, post_id, media_id, *, state="published", published_at="2026-09-07T00:02:00"):
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, published_at, "
        "ig_media_id, permalink) VALUES (?, 'feed', ?, ?, ?, ?, ?)",
        (post_id, state, published_at, published_at, media_id,
         f"https://www.instagram.com/p/{media_id}/"),
    )
    conn.commit()


def test_公開済みでmedia_idを持つ行だけを引く(db):
    _add(db, 1, "111")
    _add(db, 2, "222", state="planned")          # まだ出していない
    db.execute("INSERT INTO posts (id, kind, state, scheduled_at) "
               "VALUES (3, 'feed', 'published', '2026-09-01T00:00:00')")
    db.commit()                                   # media_id が無い

    rows = published_with_media(db)
    assert [row["id"] for row in rows] == [1]


def test_新しい順に並ぶ(db):
    _add(db, 1, "111", published_at="2026-09-05T00:01:00")
    _add(db, 2, "222", published_at="2026-09-12T00:01:00")
    assert [row["id"] for row in published_with_media(db)] == [2, 1]
    assert [row["id"] for row in published_with_media(db, limit=1)] == [2]


def test_消えた投稿はFalseで返る(monkeypatch):
    """Meta の「存在しない」は 400。本文で見分ける。"""
    def gone(*_args, **_kwargs):
        raise InstagramError(
            "Graph API が 400 を返しました: Unsupported get request. "
            "Object with ID '18118102858900398' does not exist, cannot be "
            "loaded due to missing permissions, or does not support this operation"
        )

    monkeypatch.setattr("freming.instagram.publish._request", gone)
    assert media_exists("token", "18118102858900398") is False


def test_残っていればTrueで返る(monkeypatch):
    monkeypatch.setattr("freming.instagram.publish._request",
                        lambda *a, **k: {"id": "111"})
    assert media_exists("token", "111") is True


def test_確かめられなかったときは消えた扱いにしない(monkeypatch):
    """**ここを混ぜると、トークンが切れた朝に全件が消えたように見える。**

    「存在しない」以外の失敗（通信・レート制限・権限切れ）は None。
    呼び出し側はこれを「消えた」に数えない。
    """
    def flaky(*_args, **_kwargs):
        raise InstagramError("Graph API が 190 を返しました: Session has expired")

    monkeypatch.setattr("freming.instagram.publish._request", flaky)
    assert media_exists("token", "111") is None
