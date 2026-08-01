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
