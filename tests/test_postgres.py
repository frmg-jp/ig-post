"""PostgreSQL に対する実接続テスト。

FREMING_TEST_DSN が設定されているときだけ走る。ふだんのテストは SQLite で
完結させたい（DBサーバー不要・高速）が、それだけだと「SQLite では通るが
PostgreSQL で落ちる」書き方が入り込む。CI と手元で1回ずつ実接続で確かめる。

    createdb freming_test
    FREMING_TEST_DSN=postgresql:///freming_test python -m pytest tests/test_postgres.py -q
"""

from __future__ import annotations

import os

import pytest

from freming.collect.base import Candidate
from freming.config import load_config
from freming.db import repository as R
from freming.db.connection import connect
from freming.db.migrate import migrate, status
from freming.db.transfer import TransferError, transfer

DSN = os.environ.get("FREMING_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="FREMING_TEST_DSN が未設定（PostgreSQL 実接続テストはスキップ）"
)


def _reset(dsn: str) -> None:
    conn = connect(dsn)
    try:
        for table in (
            "image_skips", "images", "jobs", "deliveries", "feedback",
            "rule_candidates", "properties", "schema_migrations",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def conn():
    _reset(DSN)
    migrate(DSN)
    connection = connect(DSN)
    yield connection
    connection.close()


def _add(conn, url="https://example.com/loft/") -> int:
    property_id = R.insert_candidate(
        conn,
        Candidate(
            source="sixsqft", source_rank="B", source_url=url,
            title="Warehouse loft", content_text="A converted warehouse.",
            for_sale_evidence="for sale", signal_score=2, is_for_sale=1,
            price="$2,400,000", location_city="Brooklyn",
            location_country="United States",
        ),
    )
    conn.commit()
    return property_id


def test_migrations_apply(conn) -> None:
    assert all(applied for _version, applied in status(DSN))


def test_duplicate_url_returns_none(conn) -> None:
    """lastrowid が無い PostgreSQL でも、重複が None で返ること。"""
    first = _add(conn)
    assert first is not None
    assert _add(conn) is None


def test_score_and_review_round_trip(conn) -> None:
    property_id = _add(conn)
    R.save_score(
        conn, property_id, score=88.4, score_reason="前歴が残る",
        score_detail='{"axes": []}', score_model="claude-sonnet-5",
        summary="1868年の倉庫", genre="adaptive_reuse", architect=None,
        year_built="1868", city=None, country=None, price=None,
        provenance_visible=True,
    )
    row = R.get_property(conn, property_id)
    assert row["score"] == pytest.approx(88.4)
    assert row["provenance_visible"] == 1
    # 収集時に入っていた値は LLM が空を返しても消えない
    assert row["location_city"] == "Brooklyn"

    assert R.approve_property(conn, property_id) is True
    assert R.count_by_status(conn)["approved"] == 1
    assert R.reject_property(conn, property_id, "前歴の痕跡が残っていない") is True
    assert R.recent_reject_reasons(conn, 5) == ["前歴の痕跡が残っていない"]


def test_delivery_queue_backoff(conn) -> None:
    """時刻比較を Python 側に寄せた箇所が、PostgreSQL でも効いていること。"""
    property_id = _add(conn)
    R.approve_property(conn, property_id)
    assert [r["id"] for r in R.delivery_queue(
        conn, limit=5, max_attempts=3, retry_after_sec=600)] == [property_id]

    R.record_delivery_failure(conn, property_id, "NoImagesFound: 画像なし")
    # 待ち時間の中なので拾わない
    assert R.delivery_queue(conn, limit=5, max_attempts=3, retry_after_sec=600) == []
    # 待ち時間ゼロなら戻ってくる
    assert [r["id"] for r in R.delivery_queue(
        conn, limit=5, max_attempts=3, retry_after_sec=0)] == [property_id]


def test_delivery_queue_gives_up_at_the_limit(conn) -> None:
    property_id = _add(conn)
    R.approve_property(conn, property_id)
    for _ in range(3):
        R.record_delivery_failure(conn, property_id, "画像なし")
    assert R.delivery_queue(conn, limit=5, max_attempts=3, retry_after_sec=0) == []
    assert R.retry_delivery(conn, property_id) is True
    assert R.delivery_queue(conn, limit=5, max_attempts=3, retry_after_sec=0)


def test_transfer_keeps_the_folder_sequence(tmp_path, conn) -> None:
    """移行で deliveries を取りこぼすと frmg_igNNN が振り直しになる。"""
    from freming.delivery.deliver import next_folder_name

    source = tmp_path / "src.db"
    migrate(source)
    src = connect(source)
    src_id = R.insert_candidate(src, Candidate(
        source="sixsqft", source_rank="B",
        source_url="https://example.com/delivered/", title="納品済み"))
    src.execute(
        "INSERT INTO deliveries (property_id, folder_name, image_count, "
        "drive_folder_id, delivered_at) VALUES (?, ?, ?, ?, ?)",
        (src_id, "frmg_ig002", 10, "abc", "2026-08-03T00:00:00+00:00"),
    )
    src.commit()
    src.close()

    _reset(DSN)
    stats = transfer(source, DSN)
    assert stats.copied["deliveries"] == 1

    dst = connect(DSN)
    try:
        cfg = load_config("config.yaml")
        assert next_folder_name(dst, cfg) == "frmg_ig003"
        # 採番の続きが合っていないと、移行後の最初の INSERT が主キー衝突で落ちる
        new_id = R.insert_candidate(dst, Candidate(
            source="x", source_rank="B",
            source_url="https://example.com/after/", title="移行後"))
        dst.commit()
        assert new_id is not None and new_id > src_id
    finally:
        dst.close()


def test_transfer_refuses_a_non_empty_destination(tmp_path, conn) -> None:
    """二重投入を防ぐ。取り込み直しは DB を作り直してから行う。"""
    source = tmp_path / "src.db"
    migrate(source)
    src = connect(source)
    R.insert_candidate(src, Candidate(
        source="x", source_rank="B",
        source_url="https://example.com/a/", title="a"))
    src.commit()
    src.close()

    _add(conn)   # 移行先に行がある状態
    with pytest.raises(TransferError, match="既に行があります"):
        transfer(source, DSN)
