"""[10] 週次レポートのテスト。

**出したものがどうだったか**を読む画面。最初は未審査の候補を並べて
いたが、それは未審査タブの仕事だった（2026-09-16 の指摘）。

確かめるのは、数字の作り方（未取得を 0 に混ぜない）と、振り返りの
チェックが「出したもの」を見ていること。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from freming.collect.base import Candidate
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import insert_candidate
from freming.report.weekly import build, render, week_bounds
from freming.web.app import create_app

# 2026-09-16（水）。この週は 09/14（月）〜 09/20（日）。
NOW = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "report.db"
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


def _add(conn, url, *, score=None, genre=None, country="United States", **extra) -> int:
    property_id = insert_candidate(
        conn,
        Candidate(
            source="wowhaus", source_rank="A", source_url=url,
            title=extra.pop("title", "A house"), content_text="...",
            is_for_sale=1, location_country=country,
            location_city=extra.pop("city", "San Francisco"),
        ),
    )
    if score is not None:
        conn.execute(
            "UPDATE properties SET score = ?, genre = ?, scored_at = ? WHERE id = ?",
            (score, genre, "2026-09-15T00:00:00+00:00", property_id),
        )
    for key, value in extra.items():
        conn.execute(f"UPDATE properties SET {key} = ? WHERE id = ?", (value, property_id))
    conn.commit()
    return property_id


def _post(conn, post_id, property_id, published_at, *, reach=None, note=None, kind="feed"):
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, published_at, property_id, "
        "reach, note) VALUES (?, ?, 'published', ?, ?, ?, ?, ?)",
        (post_id, kind, published_at, published_at, property_id, reach, note),
    )
    conn.commit()


def test_週は月曜から日曜(config) -> None:
    start, end = week_bounds(config, NOW)
    assert start.strftime("%Y-%m-%d") == "2026-09-14"   # 月曜
    assert end.strftime("%Y-%m-%d") == "2026-09-21"


def test_今週出したものが主役(config, conn) -> None:
    """未審査の候補は並べない（未審査タブの仕事）。"""
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect",
             display_name="Steel Ranch")
    b = _add(conn, "https://a.example.com/2", score=60, genre="loft",
             display_name="先週の1軒")
    _add(conn, "https://a.example.com/3", score=95, genre="hidden_gem",
         display_name="未審査の高得点")      # 出していない。レポートに出さない
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=131)
    _post(conn, 2, b, "2026-09-08T00:02:00+00:00", reach=300)   # 先週

    report = build(config, conn, NOW)
    assert [row["id"] for row in report.published] == [1]
    assert "未審査の高得点" not in render(report)


def test_未取得を0として平均に混ぜない(config, conn) -> None:
    """出したばかりの投稿はまだ集計されていない。0 扱いすると平均が下がる。"""
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    b = _add(conn, "https://a.example.com/2", score=70, genre="loft")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=200)
    _post(conn, 2, b, "2026-09-16T00:02:00+00:00")              # 未取得

    report = build(config, conn, NOW)
    assert report.reach_total == 200
    assert report.reach_avg == 200.0      # 100 ではない
    assert report.measured == 1
    check = next(c for c in report.checks if "リーチが全部読めている" in c.label)
    assert check.ok is False


def test_先週と比べる(config, conn) -> None:
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    b = _add(conn, "https://a.example.com/2", score=70, genre="loft")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=150)
    _post(conn, 2, b, "2026-09-08T00:02:00+00:00", reach=100)

    report = build(config, conn, NOW)
    assert report.prev_count == 1
    assert report.prev_total == 100
    assert report.total_delta == 50


def test_いちばん見られた1本(config, conn) -> None:
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect",
             display_name="低い方")
    b = _add(conn, "https://a.example.com/2", score=70, genre="loft",
             display_name="高い方")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=100)
    _post(conn, 2, b, "2026-09-16T00:02:00+00:00", reach=300)
    assert build(config, conn, NOW).best["id"] == 2


def test_カルテのメモが出る(config, conn) -> None:
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=100, note="1枚目が弱い")
    text = render(build(config, conn, NOW))
    assert "1枚目が弱い" in text


def test_メモが1本も無ければ要確認(config, conn) -> None:
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=100)
    check = next(c for c in build(config, conn, NOW).checks if "メモ" in c.label)
    assert check.ok is False


def test_出したものの偏りを見る(config, conn) -> None:
    """**候補ではなく、出したもの**について確かめる。"""
    for i in range(3):
        pid = _add(conn, f"https://a.example.com/{i}", score=70, genre="architect",
                   country="United States")
        _post(conn, i + 1, pid, f"2026-09-1{5 + i}T00:02:00+00:00", reach=100)

    report = build(config, conn, NOW)
    assert next(c for c in report.checks if "米国" in c.label).ok is False
    assert next(c for c in report.checks if "ジャンル" in c.label).ok is False


def test_ジャンル別の平均は直近数週で見る(config, conn) -> None:
    """1週間では1ジャンル1本になる。数週ためないと傾向にならない。"""
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    b = _add(conn, "https://a.example.com/2", score=70, genre="loft")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=300)
    _post(conn, 2, b, "2026-08-20T00:02:00+00:00", reach=100)   # 数週前でも入る

    stats = {s.genre: s for s in build(config, conn, NOW).genres}
    assert stats["architect"].reach_avg == 300
    assert stats["loft"].reach_avg == 100
    # 高い順に並べる
    assert build(config, conn, NOW).genres[0].genre == "architect"


def test_在庫は件数だけ(config, conn) -> None:
    """未審査は並べず、数とリンクだけ置く。"""
    _add(conn, "https://a.example.com/1", score=95, genre="hidden_gem",
         display_name="未審査の1軒")
    report = build(config, conn, NOW)
    assert report.pending == 1
    assert "未審査の1軒" not in render(report)


def test_テキストでも読める(config, conn) -> None:
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect",
             display_name="Grayoaks")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=131)
    text = render(build(config, conn, NOW))
    assert "WEEKLY REPORT" in text
    assert "Grayoaks" in text
    assert "いちばん見られた1本" in text


def test_画面が開く(config, conn) -> None:
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect",
             display_name="Grayoaks")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=131, note="1枚目が弱い")
    body = TestClient(create_app(config)).get("/report").text
    assert "WEEKLY REPORT" in body
    assert "Grayoaks" in body
    assert "1枚目が弱い" in body            # カルテの抜粋


def test_何も出していない週でも開く(config, conn) -> None:
    client = TestClient(create_app(config))
    assert client.get("/report?week=2026-08-03").status_code == 200


def test_壊れた週の指定でも画面は開く(config, conn) -> None:
    client = TestClient(create_app(config))
    assert client.get("/report?week=まいにち").status_code == 200


def test_画面に出る文字に星印を書かない(config, conn) -> None:
    """`**強調**` は Markdown。HTMLではそのまま星印が出る。

    2026-09-16 の実機で「**埋めるために弱い案件を入れない**」と表示されて
    いた。コード中のコメントと、画面に出る文字を混同しない。
    """
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00")
    for check in build(config, conn, NOW).checks:
        assert "**" not in check.label
        assert "**" not in check.note


# --- 投稿カルテ -------------------------------------------------------

def test_カルテに採点とリーチと写真が出る(config, conn) -> None:
    """1投稿について分かることを1画面に集める。"""
    import json

    property_id = _add(conn, "https://a.example.com/1", score=82, genre="architect",
                       display_name="Hezlep House", architect="Harold B. Zook")
    conn.execute(
        "UPDATE properties SET score_detail = ?, style_identified = 1 WHERE id = ?",
        (json.dumps({"gate": "", "axes": [
            {"key": "story", "raw": 90.0, "weight": 0.25, "reason": "設計者が特定できる"},
        ], "flags": {}}, ensure_ascii=False), property_id),
    )
    conn.execute(
        "INSERT INTO images (property_id, source_url, position, origin_url) "
        "VALUES (?, 'https://cdn.example.com/1.jpg', 1, NULL)", (property_id,),
    )
    conn.execute(
        "INSERT INTO images (property_id, source_url, position, origin_url) "
        "VALUES (?, 'https://cdn.other.org/2.jpg', 2, 'https://other.org/article')",
        (property_id,),
    )
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, published_at, property_id, "
        "reach, reach_checked_at, caption) VALUES (1, 'feed', 'published', ?, ?, ?, 384, ?, ?)",
        ("2026-09-15T00:02:00+00:00", "2026-09-15T00:02:00+00:00", property_id,
         "2026-09-16T00:00:00+00:00", "【 Hezlep House 】"),
    )
    conn.commit()

    body = TestClient(create_app(config)).get("/posts/1").text
    assert "Hezlep House" in body
    assert "384" in body                      # リーチ
    assert "設計者が特定できる" in body        # 採点の軸と理由
    assert "https://other.org/article" in body  # 他サイトから足した写真の出所


def test_リーチ未取得と0を混ぜない(config, conn) -> None:
    property_id = _add(conn, "https://a.example.com/1", score=60, genre="loft")
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, property_id) "
        "VALUES (1, 'feed', 'planned', ?, ?)",
        ("2026-09-18T00:02:00+00:00", property_id),
    )
    conn.commit()
    body = TestClient(create_app(config)).get("/posts/1").text
    assert "未取得" in body


def test_メモは保存されて公開されない(config, conn) -> None:
    property_id = _add(conn, "https://a.example.com/1", score=60, genre="loft")
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, property_id, caption) "
        "VALUES (1, 'feed', 'planned', ?, ?, '本文')",
        ("2026-09-18T00:02:00+00:00", property_id),
    )
    conn.commit()
    client = TestClient(create_app(config))
    client.post("/posts/1/note", data={"note": "1枚目が弱い"})

    row = conn.execute("SELECT note, note_at, caption FROM posts WHERE id = 1").fetchone()
    assert row["note"] == "1枚目が弱い"
    assert row["note_at"]
    assert row["caption"] == "本文"      # **本文は書き換えない**


def test_空のメモは消える(config, conn) -> None:
    property_id = _add(conn, "https://a.example.com/1", score=60, genre="loft")
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, property_id, note) "
        "VALUES (1, 'feed', 'planned', ?, ?, '前のメモ')",
        ("2026-09-18T00:02:00+00:00", property_id),
    )
    conn.commit()
    TestClient(create_app(config)).post("/posts/1/note", data={"note": "   "})
    assert conn.execute("SELECT note FROM posts WHERE id = 1").fetchone()["note"] is None


def test_無い投稿は404(config) -> None:
    assert TestClient(create_app(config)).get("/posts/999").status_code == 404
