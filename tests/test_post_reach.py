"""リーチを**記録する**経路のテスト。

2026-09-15 まで、週次リールの選抜で毎週リーチを読んでいたのに、DBには
1件も残っていなかった（record_reach を誰も呼んでいなかった）。記録が
無いので「先週と比べてどうだったか」が一度も言えていない。
"""

from __future__ import annotations

import pytest

from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import (
    posts_for_reach,
    record_reach,
    record_reach_by_media,
)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "reach.db"
    migrate(path)
    conn = connect(path)
    yield conn
    conn.close()


def _add(conn, post_id, media_id, published_at, *, state="published"):
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, published_at, ig_media_id) "
        "VALUES (?, 'feed', ?, ?, ?, ?)",
        (post_id, state, published_at, published_at, media_id),
    )
    conn.commit()


def test_読み直す対象は期間内の公開済みだけ(db):
    _add(db, 1, "111", "2026-09-14T00:02:00+00:00")
    _add(db, 2, "222", "2026-08-01T00:02:00+00:00")   # 古い
    _add(db, 3, "333", "2026-09-15T00:02:00+00:00", state="planned")
    rows = posts_for_reach(db, "2026-09-01T00:00:00+00:00")
    assert [row["id"] for row in rows] == [1]


def test_新しい順に読む(db):
    _add(db, 1, "111", "2026-09-10T00:02:00+00:00")
    _add(db, 2, "222", "2026-09-14T00:02:00+00:00")
    assert [r["id"] for r in posts_for_reach(db, "2026-09-01T00:00:00+00:00")] == [2, 1]


def test_リーチを記録すると読んだ時刻も残る(db):
    _add(db, 1, "111", "2026-09-14T00:02:00+00:00")
    record_reach(db, 1, 384)
    row = db.execute("SELECT reach, reach_checked_at FROM posts WHERE id = 1").fetchone()
    assert row["reach"] == 384
    assert row["reach_checked_at"]


def test_media_idから引いて記録できる(db):
    """週次リールの選抜は**アカウントの実物**を見るので post_id を知らない。"""
    _add(db, 1, "111", "2026-09-14T00:02:00+00:00")
    assert record_reach_by_media(db, "111", 294) is True
    assert db.execute("SELECT reach FROM posts WHERE id = 1").fetchone()["reach"] == 294


def test_予定表に無い投稿は何もしない(db):
    """手で出した投稿は posts に行が無い。落とさず、書かずに済ませる。"""
    _add(db, 1, "111", "2026-09-14T00:02:00+00:00")
    assert record_reach_by_media(db, "999", 100) is False
    assert db.execute("SELECT reach FROM posts WHERE id = 1").fetchone()["reach"] is None


def test_リールの選抜がリーチを保存する(db, monkeypatch):
    """**読んだ数字をその場で捨てない。** ここが抜けていた。"""
    from datetime import UTC, datetime

    from freming.config import load_config
    from freming.instagram import worker

    _add(db, 1, "111", "2026-09-08T00:02:00+00:00")
    cfg = load_config("config.yaml")

    class _Item:
        id = "111"
        timestamp = "2026-09-08T00:02:00+0000"
        image_url = "https://cdn.example.com/1.jpg"
        media_type = "IMAGE"
        caption = "【 Steel Ranch 】"

        def head(self) -> str:
            return "Steel Ranch"

    monkeypatch.setattr(
        "freming.instagram.mymedia.recent_media", lambda *a, **k: [_Item()]
    )
    monkeypatch.setattr(worker, "media_reach", lambda *a, **k: 384)

    picks, _picked_by = worker.weekly_picks(
        cfg, db, "token", "ig", datetime(2026, 9, 14, 10, 0, tzinfo=UTC),
    )
    assert [p.reach for p in picks] == [384]
    assert db.execute("SELECT reach FROM posts WHERE id = 1").fetchone()["reach"] == 384
