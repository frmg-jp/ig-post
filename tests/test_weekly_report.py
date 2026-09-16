"""[10] 週次レポートのテスト。

編集方針（WEEKLY GLOBAL ARCHITECTURE REPORT）の形に、手元の候補を
並べ直すもの。**外へは出ない**ので、確かめるのはDBの読み方と、
足りないことを隠さずに出すかどうか。
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


def test_週は月曜から日曜(config) -> None:
    start, end = week_bounds(config, NOW)
    assert start.strftime("%Y-%m-%d") == "2026-09-14"   # 月曜
    assert end.strftime("%Y-%m-%d") == "2026-09-21"


def test_候補は未審査の点数順(config, conn) -> None:
    _add(conn, "https://a.example.com/1", score=70, genre="architect")
    _add(conn, "https://a.example.com/2", score=90, genre="hidden_gem")
    low = _add(conn, "https://a.example.com/3", score=40, genre="loft")
    conn.execute("UPDATE properties SET status = 'approved' WHERE id = ?", (low,))
    conn.commit()

    report = build(config, conn, NOW)
    assert [float(r["score"]) for r in report.candidates] == [90.0, 70.0]
    # 審査済みは候補に出さない（もう選び終わっている）
    assert low not in [r["id"] for r in report.candidates]


def test_採点していない候補は出さない(config, conn) -> None:
    """点が無いものを混ぜると、並びの意味が無くなる。"""
    _add(conn, "https://a.example.com/1", score=60, genre="architect")
    _add(conn, "https://a.example.com/2")      # 未採点
    report = build(config, conn, NOW)
    assert len(report.candidates) == 1


def test_ジャンル別に並ぶ(config, conn) -> None:
    _add(conn, "https://a.example.com/1", score=80, genre="adaptive_reuse")
    _add(conn, "https://a.example.com/2", score=70, genre="architect")
    report = build(config, conn, NOW)
    labels = [s.label for s in report.sections]
    # config の優先順（architect が先）に従う。点数順ではない
    assert labels[0].startswith("Architect")
    assert "Conversion" in labels[1]


def test_Pickは最上位の1件(config, conn) -> None:
    _add(conn, "https://a.example.com/1", score=70, genre="architect")
    top = _add(conn, "https://a.example.com/2", score=95, genre="hidden_gem")
    assert build(config, conn, NOW).pick["id"] == top


def test_件数が足りないことを隠さない(config, conn) -> None:
    """**10件を埋めるために弱い案件を入れない**が方針。ただし黙らない。"""
    _add(conn, "https://a.example.com/1", score=70, genre="architect")
    report = build(config, conn, NOW)
    check = next(c for c in report.checks if "10 件" in c.label)
    assert check.ok is False
    assert "弱い案件を入れない" in check.note


def test_米国への偏りを出す(config, conn) -> None:
    for i in range(3):
        _add(conn, f"https://a.example.com/{i}", score=70, genre="architect",
             country="United States")
    report = build(config, conn, NOW)
    check = next(c for c in report.checks if "米国" in c.label)
    assert check.ok is False
    assert "3 件" in check.note


def test_偏りが無ければ通る(config, conn) -> None:
    _add(conn, "https://a.example.com/1", score=70, genre="architect",
         country="United States")
    _add(conn, "https://a.example.com/2", score=60, genre="adaptive_reuse",
         country="France")
    report = build(config, conn, NOW)
    assert next(c for c in report.checks if "米国" in c.label).ok is True
    assert next(c for c in report.checks if "Conversion" in c.label).ok is True
    assert next(c for c in report.checks if "Hidden Gem" in c.label).ok is False


def test_今週出した投稿とリーチが出る(config, conn) -> None:
    property_id = _add(conn, "https://a.example.com/1", score=70, genre="architect",
                       display_name="Steel Ranch")
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, published_at, property_id, reach) "
        "VALUES (1, 'feed', 'published', ?, ?, ?, 384)",
        ("2026-09-15T00:02:00+00:00", "2026-09-15T00:02:00+00:00", property_id),
    )
    # 先週の投稿は入らない（別の物件。posts は物件×種別で1行）
    other = _add(conn, "https://a.example.com/2", score=60, genre="loft")
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, published_at, property_id) "
        "VALUES (2, 'feed', 'published', ?, ?, ?)",
        ("2026-09-08T00:02:00+00:00", "2026-09-08T00:02:00+00:00", other),
    )
    conn.commit()

    report = build(config, conn, NOW)
    assert [row["id"] for row in report.published] == [1]
    assert report.reach_total == 384


def test_テキストでも読める(config, conn) -> None:
    _add(conn, "https://a.example.com/1", score=70, genre="architect",
         display_name="Grayoaks")
    text = render(build(config, conn, NOW))
    assert "WEEKLY REPORT" in text
    assert "Grayoaks" in text
    assert "Pick of the Week" in text


def test_画面が開く(config, conn) -> None:
    _add(conn, "https://a.example.com/1", score=70, genre="architect",
         display_name="Grayoaks")
    client = TestClient(create_app(config))
    body = client.get("/report").text
    assert "WEEKLY REPORT" in body
    assert "Grayoaks" in body


def test_壊れた週の指定でも画面は開く(config, conn) -> None:
    client = TestClient(create_app(config))
    assert client.get("/report?week=まいにち").status_code == 200


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
