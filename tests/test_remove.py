"""候補の削除（cli remove）の検証。

本番DBに対する破壊的操作なので、
  - 納品済みは絶対に消えないこと
  - --dry-run が本当に消さないこと
  - 消す対象の選び方（ソース単位 / 写真なし）
を固定しておく。
"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from freming.collect.base import Candidate
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import (
    deletable_properties,
    delete_properties,
    insert_candidate,
    properties_for_photo_audit,
)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    return connect(path)


def _add(conn, source: str, url: str, *, thumbnail=None, status="pending") -> int:
    property_id = insert_candidate(
        conn,
        Candidate(
            source=source,
            source_rank="B",
            source_url=url,
            title=f"{source} の物件",
            content_text="x",
            for_sale_evidence="y",
            signal_score=None,
            price="$1",
            location_city=None,
            location_country=None,
            is_for_sale=1,
            thumbnail_url=thumbnail,
        ),
    )
    if status != "pending":
        conn.execute("UPDATE properties SET status = ? WHERE id = ?", (status, property_id))
    conn.commit()
    return property_id


def test_delete_by_source(db) -> None:
    _add(db, "hbhousing", "https://x.example.com/1")
    _add(db, "hbhousing", "https://x.example.com/2")
    keep = _add(db, "dezeen", "https://x.example.com/3")

    assert delete_properties(db, source="hbhousing") == 2
    remaining = [r["id"] for r in db.execute("SELECT id FROM properties").fetchall()]
    assert remaining == [keep]


def test_delivered_rows_are_never_deleted(db) -> None:
    """納品済みは消さない。消すと同じ物件が再収集されて二重納品になる。"""
    delivered = _add(db, "hbhousing", "https://x.example.com/1", status="delivered")
    _add(db, "hbhousing", "https://x.example.com/2")

    assert delete_properties(db, source="hbhousing") == 1
    left = [r["id"] for r in db.execute("SELECT id FROM properties").fetchall()]
    assert left == [delivered]


def test_deletable_properties_matches_what_delete_removes(db) -> None:
    """--dry-run で見せた対象と、実際に消えるものが食い違わないこと。"""
    _add(db, "hbhousing", "https://x.example.com/1")
    _add(db, "hbhousing", "https://x.example.com/2", status="delivered")

    preview = [r["id"] for r in deletable_properties(db, source="hbhousing")]
    assert len(preview) == 1
    assert delete_properties(db, source="hbhousing") == len(preview)


def test_delete_by_ids_skips_delivered(db) -> None:
    a = _add(db, "dreamtown", "https://x.example.com/1")
    b = _add(db, "dreamtown", "https://x.example.com/2", status="delivered")

    assert delete_properties(db, ids=[a, b]) == 1
    left = [r["id"] for r in db.execute("SELECT id FROM properties").fetchall()]
    assert left == [b]


def test_empty_id_list_deletes_nothing(db) -> None:
    """写真の検査で1件も該当しなかったとき、全件消したりしないこと。"""
    _add(db, "dreamtown", "https://x.example.com/1")
    assert delete_properties(db, ids=[]) == 0
    assert deletable_properties(db, ids=[]) == []
    assert db.execute("SELECT COUNT(*) AS n FROM properties").fetchone()["n"] == 1


def test_delete_without_any_condition_is_refused(db) -> None:
    """条件を渡し忘れたときに全件消さない。"""
    _add(db, "dreamtown", "https://x.example.com/1")
    with pytest.raises(ValueError):
        delete_properties(db)


def test_photo_audit_skips_delivered(db) -> None:
    _add(db, "dreamtown", "https://x.example.com/1")
    _add(db, "dreamtown", "https://x.example.com/2", status="delivered")

    audited = properties_for_photo_audit(db)
    assert [r["source_url"] for r in audited] == ["https://x.example.com/1"]


def test_photo_audit_returns_the_thumbnail_url(db) -> None:
    """写真の検査は URL の有無では決められない。実物を取りに行くための材料を返す。"""
    _add(db, "dreamtown", "https://x.example.com/1", thumbnail="https://p.example.com/1.jpg")
    row = properties_for_photo_audit(db)[0]
    assert row["thumbnail_url"] == "https://p.example.com/1.jpg"


def test_flat_thumbnail_is_what_the_audit_looks_for() -> None:
    """検査が使う判定そのもの。Dream Town の実物と同じ 1280x800 単色。"""
    from freming.images.placeholder import is_flat_image

    buffer = BytesIO()
    Image.new("RGB", (1280, 800), (208, 208, 208)).save(buffer, "JPEG", quality=90)
    assert is_flat_image(buffer.getvalue())
