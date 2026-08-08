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


# --- 再納品 / 再採点 / ソース別実績 ------------------------------------
def test_納品を取り消すと承認済みに戻る(db):
    """Drive のフォルダは消さない。人が片付けてから呼ぶ前提。"""
    from freming.db.repository import undo_delivery

    property_id = _add(db, "dezeen", "https://example.com/x", status="delivered")
    db.execute(
        "INSERT INTO deliveries (property_id, folder_name, image_count, "
        "drive_folder_id, delivered_at) VALUES (?, 'frmg_ig006', 8, 'drv1', ?)",
        (property_id, "2026-08-01T00:00:00+00:00"),
    )
    db.execute("UPDATE properties SET delivery_attempts = 3 WHERE id = ?", (property_id,))
    db.commit()

    assert undo_delivery(db, property_id) == "frmg_ig006"
    row = db.execute(
        "SELECT status, delivery_attempts FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    assert row["status"] == "approved"
    assert row["delivery_attempts"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"] == 0


def test_納品していないものは取り消せない(db):
    from freming.db.repository import undo_delivery

    property_id = _add(db, "dezeen", "https://example.com/y")
    assert undo_delivery(db, property_id) is None


def test_採点し直す対象に納品済みは入らない(db):
    """既に人が承認して外に出したものを、あとからルールで落とさない。"""
    from freming.db.repository import properties_needing_rescore

    kept = _add(db, "dezeen", "https://example.com/a", status="pending")
    done = _add(db, "dezeen", "https://example.com/b", status="delivered")
    for pid in (kept, done):
        db.execute(
            "UPDATE properties SET score = 60, scored_at = ? WHERE id = ?",
            ("2026-08-01T00:00:00+00:00", pid),
        )
    db.commit()
    ids = [r["id"] for r in properties_needing_rescore(db)]
    assert kept in ids
    assert done not in ids


def test_未採点のものは対象にならない(db):
    from freming.db.repository import properties_needing_rescore

    _add(db, "dezeen", "https://example.com/c")
    assert properties_needing_rescore(db) == []


def test_採点日時で絞れる(db):
    from freming.db.repository import properties_needing_rescore

    old = _add(db, "dezeen", "https://example.com/old")
    new = _add(db, "dezeen", "https://example.com/new")
    db.execute("UPDATE properties SET score = 60, scored_at = ? WHERE id = ?",
               ("2026-08-01T00:00:00+00:00", old))
    db.execute("UPDATE properties SET score = 60, scored_at = ? WHERE id = ?",
               ("2026-08-06T00:00:00+00:00", new))
    db.commit()
    ids = [r["id"] for r in properties_needing_rescore(db, before="2026-08-05")]
    assert ids == [old]


def test_採点を消すと未採点に戻る(db):
    from freming.db.repository import clear_scores

    property_id = _add(db, "dezeen", "https://example.com/d")
    db.execute("UPDATE properties SET score = 60, scored_at = ? WHERE id = ?",
               ("2026-08-01T00:00:00+00:00", property_id))
    db.commit()
    assert clear_scores(db, [property_id]) == 1
    row = db.execute("SELECT score, scored_at FROM properties WHERE id = ?",
                     (property_id,)).fetchone()
    assert row["score"] is None
    assert row["scored_at"] is None


def test_ソース別の実績が出る(db):
    from freming.db.repository import source_outcomes

    _add(db, "dezeen", "https://a.example.com/1", status="delivered")
    _add(db, "dezeen", "https://a.example.com/2", status="rejected")
    _add(db, "vanguard", "https://b.example.com/1", status="rejected")
    rows = {r["source"]: r for r in source_outcomes(db)}
    assert rows["dezeen"]["collected"] == 2
    assert rows["dezeen"]["delivered"] == 1
    assert rows["vanguard"]["rejected"] == 1
    assert rows["vanguard"]["delivered"] == 0
