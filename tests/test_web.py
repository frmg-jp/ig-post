"""[3] 審査UI のテスト。

承認・非承認が DB に正しく反映され、非承認理由が feedback に残ることを
確かめる。理由の蓄積が [7] 学習ループの入力なので、ここが抜けると
学習が回らない。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from freming.collect.base import Candidate
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import insert_candidate
from freming.web.app import create_app


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "review.db"
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


@pytest.fixture()
def client(config):
    return TestClient(create_app(config))


def _add(conn, url="https://example.com/loft/", **overrides) -> int:
    data = {
        "source": "wowhaus",
        "source_rank": "A",
        "source_url": url,
        "title": "Former warehouse loft",
        "content_text": "A converted warehouse.",
        "for_sale_evidence": "for sale",
        "signal_score": 2,
        "is_for_sale": 1,
        "price": "$2,400,000",
        "location_city": "San Francisco",
        "location_country": "United States",
        "thumbnail_url": None,
    }
    data.update(overrides)
    property_id = insert_candidate(conn, Candidate(**data))
    conn.commit()
    return property_id


def _status(conn, property_id) -> str:
    return conn.execute(
        "SELECT status FROM properties WHERE id = ?", (property_id,)
    ).fetchone()["status"]


def test_list_shows_pending_candidates(client, conn) -> None:
    _add(conn)
    body = client.get("/").text
    assert "Former warehouse loft" in body
    assert "承認" in body


def test_approve_updates_status(client, conn) -> None:
    property_id = _add(conn)
    response = client.post(f"/p/{property_id}/approve", data={"status": "pending"})
    assert response.status_code == 200      # リダイレクト先まで辿った結果
    assert _status(conn, property_id) == "approved"


def test_reject_records_feedback(client, conn) -> None:
    """非承認理由が feedback に入ること（スコアリングの学習材料になる）。"""
    property_id = _add(conn)
    client.post(
        f"/p/{property_id}/reject",
        data={"reason": "前歴の痕跡が残っていない（内装だけのリノベ）", "status": "pending"},
    )
    assert _status(conn, property_id) == "rejected"

    rows = conn.execute("SELECT reason, property_id FROM feedback").fetchall()
    assert len(rows) == 1
    assert "痕跡が残っていない" in rows[0]["reason"]
    assert rows[0]["property_id"] == property_id


def test_reject_combines_preset_and_free_text(client, conn) -> None:
    property_id = _add(conn)
    client.post(
        f"/p/{property_id}/reject",
        data={"reason": "様式・築年が特定できない", "reason_free": "1990年代の建売に見える"},
    )
    reason = conn.execute("SELECT reason FROM feedback").fetchone()["reason"]
    assert "様式・築年が特定できない" in reason
    assert "1990年代の建売に見える" in reason


def test_reject_without_reason_is_refused(client, conn) -> None:
    """理由なしの非承認は受け付けない。学習の材料が失われるため。"""
    property_id = _add(conn)
    client.post(f"/p/{property_id}/reject", data={"reason": "", "reason_free": "  "})
    assert _status(conn, property_id) == "pending"
    assert conn.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()["n"] == 0


def test_delivered_candidates_cannot_be_changed(client, conn) -> None:
    """納品済みは審査し直せない（重複納品を防ぐ）。"""
    property_id = _add(conn)
    conn.execute("UPDATE properties SET status = 'delivered' WHERE id = ?", (property_id,))
    conn.commit()

    client.post(f"/p/{property_id}/approve")
    client.post(f"/p/{property_id}/reject", data={"reason": "やっぱり違う"})
    assert _status(conn, property_id) == "delivered"


def test_reset_returns_to_pending_but_keeps_feedback(client, conn) -> None:
    """誤操作の復旧。人が下した判断そのものは学習材料として残す。"""
    property_id = _add(conn)
    client.post(f"/p/{property_id}/reject", data={"reason": "画像が足りない、または品質が低い"})
    client.post(f"/p/{property_id}/reset")

    assert _status(conn, property_id) == "pending"
    assert conn.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()["n"] == 1


def test_detail_page_shows_collected_text(client, conn) -> None:
    property_id = _add(conn, content_text="Ghost signage remains on the brick facade.")
    body = client.get(f"/p/{property_id}").text
    assert "Ghost signage remains" in body


def test_unknown_property_returns_404(client) -> None:
    assert client.get("/p/9999").status_code == 404


def test_manual_entry_does_not_fetch_the_page(client, conn, monkeypatch) -> None:
    """手動投入では相手サイトに一切アクセスしないこと。"""
    from freming.collect import manual as manual_mod

    def _boom(*_args, **_kwargs):
        raise AssertionError("手動投入でHTTPクライアントを作ってはいけない")

    monkeypatch.setattr(manual_mod, "HttpClient", _boom)

    client.post(
        "/manual",
        data={
            "url": "https://www.zillow.com/homedetails/123/",
            "title": "Firehouse conversion",
            "price": "$1,250,000",
            "city": "Chicago",
        },
    )
    row = conn.execute(
        "SELECT * FROM properties WHERE source_url LIKE '%zillow%'"
    ).fetchone()
    assert row is not None
    assert row["is_for_sale"] == 1
    assert "手動入力" in row["for_sale_evidence"]


def test_score_breakdown_is_rendered(client, conn) -> None:
    """軸ごとの内訳が画面に出ること（なぜその点数かを人が確認できる）。"""
    property_id = _add(conn)
    conn.execute(
        "UPDATE properties SET score = 87.3, score_detail = ? WHERE id = ?",
        (
            '{"axes": [{"key": "story", "raw": 90, "weight": 0.25, "reason": ""},'
            ' {"key": "area", "raw": 100, "weight": 0.2, "reason": "San Francisco"}]}',
            property_id,
        ),
    )
    conn.commit()

    body = client.get("/").text
    assert "87.3" in body
    assert "story=90" in body
    assert "San Francisco" in body


def test_broken_score_detail_does_not_break_the_page(client, conn) -> None:
    """壊れた内訳データで一覧全体が落ちないこと。"""
    property_id = _add(conn)
    conn.execute(
        "UPDATE properties SET score = 50, score_detail = 'not json' WHERE id = ?",
        (property_id,),
    )
    conn.commit()
    assert client.get("/").status_code == 200
