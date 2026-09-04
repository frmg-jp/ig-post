"""マイグレーションと重複防止制約の検証。"""

from __future__ import annotations

import sqlite3

import pytest

from freming.db.connection import connect
from freming.db.migrate import migrate, status

EXPECTED_TABLES = {
    "properties",
    "feedback",
    "deliveries",
    "images",
    "jobs",
    "rule_candidates",
}


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    return path


def test_creates_all_tables(db) -> None:
    conn = connect(db)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names
    conn.close()


def test_migrate_is_idempotent(db) -> None:
    assert migrate(db) == []  # 2回目は何も適用されない
    assert all(applied for _, applied in status(db))


def test_source_url_is_unique(db) -> None:
    """再実行しても同じ記事が二重登録されないこと。"""
    conn = connect(db)
    conn.execute(
        "INSERT INTO properties (source, source_url, title) VALUES (?, ?, ?)",
        ("dezeen", "https://example.com/a", "A"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO properties (source, source_url, title) VALUES (?, ?, ?)",
            ("dezeen", "https://example.com/a", "A（重複）"),
        )
    conn.close()


def test_delivery_is_unique_per_property(db) -> None:
    """同じ物件を二重に納品できないこと。"""
    conn = connect(db)
    conn.execute(
        "INSERT INTO properties (id, source, source_url) VALUES (1, 'dezeen', 'https://x/1')"
    )
    conn.execute(
        "INSERT INTO deliveries (property_id, folder_name, image_count) VALUES (1, 'frmg_ig001', 8)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO deliveries (property_id, folder_name, image_count) "
            "VALUES (1, 'frmg_ig002', 8)"
        )
    conn.close()


def test_folder_name_is_unique(db) -> None:
    conn = connect(db)
    conn.executescript(
        "INSERT INTO properties (id, source, source_url) VALUES (1, 'dezeen', 'https://x/1');"
        "INSERT INTO properties (id, source, source_url) VALUES (2, 'dezeen', 'https://x/2');"
        "INSERT INTO deliveries (property_id, folder_name) VALUES (1, 'frmg_ig001');"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO deliveries (property_id, folder_name) VALUES (2, 'frmg_ig001')")
    conn.close()


def test_images_record_source_url_and_credit(db) -> None:
    """画像は取得元URLを必ず持ち、同一URLの重複登録を防ぐこと。"""
    conn = connect(db)
    conn.execute("INSERT INTO properties (id, source, source_url) VALUES (1, 'dezeen', 'https://x/1')")
    conn.execute(
        "INSERT INTO images (property_id, source_url, credit, position) VALUES (1, ?, ?, 1)",
        ("https://example.com/photo.jpg", "Photo: Someone"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO images (property_id, source_url, position) VALUES (1, ?, 2)",
            ("https://example.com/photo.jpg",),
        )
    conn.close()


def test_pending_migrations_are_reported_before_use(tmp_path) -> None:
    """列が足りないまま動かさず、何をすればよいかを出すこと。

    migration を足したあと db migrate を忘れると、審査UIの全画面が
    「no such column」で500になる。原因が分かりにくいので手前で止める。
    """
    from freming.db.migrate import PendingMigrations, ensure_migrated

    missing = tmp_path / "not-created-yet.db"
    with pytest.raises(PendingMigrations) as exc:
        ensure_migrated(missing)
    assert "db migrate" in str(exc.value)

    # 適用済みなら通る
    path = tmp_path / "ok.db"
    migrate(path)
    ensure_migrated(path)


def test_0019は既存の判定をJSONから埋め戻す(db) -> None:
    """**採点をやり直さずに列を埋める。**

    style_identified / one_of_a_kind の値は 106件ぶんすでに score_detail
    の中にある。埋め戻さないと、列を足しても既存の行は全部 NULL のままで、
    絞り込みにも検証にも使えない——APIに再課金して採点し直すことになる。
    """
    import json

    from freming.db.migrate import discover_migrations

    conn = connect(db)
    detail = json.dumps(
        {"gate": "", "axes": [],
         "flags": {"provenance_visible": False,
                   "style_identified": True, "one_of_a_kind": False}},
        ensure_ascii=False,
    )
    conn.execute(
        "INSERT INTO properties (source, source_url, title, status, score, score_detail)"
        " VALUES ('dwell', 'https://example.com/a/', 'A', 'approved', 70, ?)",
        (detail,),
    )
    # 採点前の行（score_detail が無い）は触らない
    conn.execute(
        "INSERT INTO properties (source, source_url, title, status)"
        " VALUES ('dwell', 'https://example.com/b/', 'B', 'pending')"
    )
    conn.commit()

    # 埋め戻す前は空。これを確かめておかないと、下の assert が
    # 「もともと入っていた」のか「埋め戻された」のか区別できない。
    before = conn.execute(
        "SELECT style_identified FROM properties WHERE title = 'A'").fetchone()
    assert before["style_identified"] is None

    # 0019 の埋め戻しはマイグレーション適用時に一度走るだけなので、
    # 既存行に対する効き方はSQLをそのまま流して確かめる。
    sql = next(m for m in discover_migrations() if m.version.startswith("0019")).sql
    update = "UPDATE properties" + sql.split("UPDATE properties", 1)[1].split(";", 1)[0] + ";"
    conn.executescript(update)
    conn.commit()

    rows = {r["title"]: r for r in conn.execute(
        "SELECT title, style_identified, one_of_a_kind FROM properties")}
    assert rows["A"]["style_identified"] == 1
    assert rows["A"]["one_of_a_kind"] == 0        # false は 0。NULL にしない
    assert rows["B"]["style_identified"] is None  # 採点前は不明のまま
    conn.close()
