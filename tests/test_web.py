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


def test_rules_page_needs_explicit_approval(client, conn) -> None:
    """ルール候補は画面で承認するまで適用されないこと。"""
    from freming.db.repository import approved_rules

    conn.execute(
        "INSERT INTO rule_candidates (reason_tag, hit_count, proposal, state, created_at) "
        "VALUES ('no_visible_provenance', 4, '痕跡が残っていない物件は対象外とする', "
        "'proposed', datetime('now'))"
    )
    conn.commit()

    body = client.get("/rules").text
    assert "痕跡が残っていない物件は対象外とする" in body
    assert approved_rules(conn) == []

    client.post("/rules/no_visible_provenance/approve")
    assert approved_rules(conn) == ["痕跡が残っていない物件は対象外とする"]


def test_dismissing_a_rule_keeps_it_out_of_the_prompt(client, conn) -> None:
    from freming.db.repository import approved_rules

    conn.execute(
        "INSERT INTO rule_candidates (reason_tag, hit_count, proposal, state, created_at) "
        "VALUES ('area_mismatch', 5, 'エリア外は対象外とする', 'proposed', datetime('now'))"
    )
    conn.commit()

    client.post("/rules/area_mismatch/dismiss")
    assert approved_rules(conn) == []


def test_series_is_applied_by_a_human(client, conn) -> None:
    """連載企画は審査UIで人が付けること（スコアリングでは決めない）。"""
    property_id = _add(conn)

    body = client.get("/").text
    assert "FREMING Pick" in body          # 選択肢が出ている

    client.post(f"/p/{property_id}/series", data={"series": "freming_pick"})
    row = conn.execute(
        "SELECT series FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    assert row["series"] == "freming_pick"


def test_series_can_be_cleared(client, conn) -> None:
    property_id = _add(conn)
    client.post(f"/p/{property_id}/series", data={"series": "hidden_gem"})
    client.post(f"/p/{property_id}/series", data={"series": ""})

    row = conn.execute(
        "SELECT series FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    assert row["series"] is None


def test_unknown_series_key_is_rejected(client, conn) -> None:
    """config.yaml に無い企画キーは保存しない。"""
    property_id = _add(conn)
    client.post(f"/p/{property_id}/series", data={"series": "made_up_series"})

    row = conn.execute(
        "SELECT series FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    assert row["series"] is None


def test_delivered_series_cannot_be_changed(client, conn) -> None:
    """納品済みのラベルは変えない（meta.txt と食い違うため）。"""
    property_id = _add(conn)
    conn.execute(
        "UPDATE properties SET status = 'delivered', series = 'hidden_gem' WHERE id = ?",
        (property_id,),
    )
    conn.commit()

    client.post(f"/p/{property_id}/series", data={"series": "freming_pick"})
    row = conn.execute(
        "SELECT series FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    assert row["series"] == "hidden_gem"


def test_list_can_filter_by_series(client, conn) -> None:
    picked = _add(conn, url="https://example.com/a/", title="Picked loft")
    other = _add(conn, url="https://example.com/b/", title="Other loft")
    client.post(f"/p/{picked}/series", data={"series": "freming_pick"})

    body = client.get("/?status=pending&series=freming_pick").text
    assert "Picked loft" in body
    assert "Other loft" not in body
    assert other  # 未使用変数の警告避け


def test_empty_approved_tab_explains_what_to_do(client) -> None:
    """空の画面で、そのタブに出すための次の一手が分かること。"""
    body = client.get("/?status=approved").text
    assert "承認済みの物件はまだありません" in body
    assert "未審査" in body


def test_empty_pending_tab_suggests_collecting(client) -> None:
    body = client.get("/?status=pending").text
    assert "collect" in body and "score" in body


def test_empty_series_filter_explains_labels(client) -> None:
    body = client.get("/?status=pending&series=freming_pick").text
    assert "この企画のラベル" in body
