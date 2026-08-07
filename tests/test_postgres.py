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
    """テスト用DBを空にする。

    **消す表を列挙しない。** 以前はここに固定の一覧を書いていたが、
    マイグレーションでテーブルが増えるたびに古くなる。実際、0009 で
    fx_rates を足したときに漏れ、schema_migrations だけが消えて
    fx_rates が残ったため、次の migrate が
    `relation "fx_rates" already exists` で落ちてCIが赤になった。

    列挙する限り同じことが起きるので、実際にあるものを問い合わせて消す。
    """
    conn = connect(dsn)
    try:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        for row in rows:
            # 表名は pg_tables から来た実在の識別子。念のため引用符で囲む。
            conn.execute(f'DROP TABLE IF EXISTS "{row["tablename"]}" CASCADE')
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


def test_reset_leaves_no_tables_behind() -> None:
    """後始末が取りこぼさないこと。

    以前は消す表を固定で列挙していて、マイグレーションで増えるたびに
    古くなった。0009 で fx_rates を足したときに漏れ、schema_migrations
    だけが消えて fx_rates が残り、次の migrate が
    `relation "fx_rates" already exists` で落ちてCIが赤になった。
    """
    migrate(DSN)
    _reset(DSN)

    conn = connect(DSN)
    try:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    finally:
        conn.close()
    assert [r["tablename"] for r in rows] == []


def test_migrate_survives_a_reset_and_runs_again() -> None:
    """落として作り直しても migrate が通ること（CIが毎回やっている手順）。"""
    for _ in range(2):
        _reset(DSN)
        migrate(DSN)
        assert all(applied for _, applied in status(DSN))


# --- [9] 投稿 ---------------------------------------------------------
def test_投稿する画像がPostgreSQLでも往復する(conn):
    """BLOB は PostgreSQL に無い。dialect が BYTEA に寄せていること。

    SQLite だけで確かめると、bytes の受け渡しが本番で壊れていても
    気づけない（psycopg は bytea を memoryview で返す）。
    """
    from freming.db.repository import create_post
    from freming.instagram.media import load_media, purge_media, store_media

    post_id = create_post(conn, "feed", "2026-08-10T01:00:00+00:00")
    payload = bytes(range(256)) * 8
    token = store_media(conn, post_id, payload)

    found = load_media(conn, token)
    assert found is not None
    content, mime = found
    assert isinstance(content, bytes)
    assert content == payload
    assert mime == "image/jpeg"
    assert purge_media(conn, post_id) == 1


def test_同じ予定をPostgreSQLでも二度は取れない(conn):
    """claim_due_post の UPDATE ... RETURNING が両方言で効くこと。"""
    from freming.db.repository import claim_due_post, create_post

    create_post(conn, "feed", "2026-08-10T01:00:00+00:00")
    assert claim_due_post(conn, "2026-08-10T02:00:00+00:00", 3) is not None
    assert claim_due_post(conn, "2026-08-10T02:00:00+00:00", 3) is None


def test_リールはproperty_idがNULLでも複数置ける(conn):
    """UNIQUE(property_id, kind) に NULL 同士が引っかからないこと。"""
    from freming.db.repository import create_post

    assert create_post(conn, "reel", "2026-08-10T10:00:00+00:00") is not None
    assert create_post(conn, "reel", "2026-08-17T10:00:00+00:00") is not None
