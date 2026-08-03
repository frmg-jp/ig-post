"""SQLite と PostgreSQL の差を吸収する層のテスト。

ここが壊れると、SQLite のテストは通るのに本番（PostgreSQL）だけ落ちる。
差の入り口はプレースホルダと DDL の2つしかないので、両方を押さえる。
"""

from __future__ import annotations

from pathlib import Path

from freming.db.dialect import (
    POSTGRES,
    SQLITE,
    dialect_of,
    redact,
    to_paramstyle,
    to_postgres_ddl,
    translate_ddl,
)


def test_sqlite_path_is_sqlite() -> None:
    assert dialect_of(Path("data/freming.db")) == SQLITE
    assert dialect_of("data/freming.db") == SQLITE


def test_postgres_urls_are_recognised() -> None:
    assert dialect_of("postgresql://u:p@host/db") == POSTGRES
    assert dialect_of("postgres://u:p@host/db") == POSTGRES


def test_placeholders_become_percent_s() -> None:
    sql = "SELECT * FROM properties WHERE id = ? AND status = ?"
    assert to_paramstyle(sql, POSTGRES) == (
        "SELECT * FROM properties WHERE id = %s AND status = %s"
    )


def test_sqlite_is_left_alone() -> None:
    sql = "SELECT * FROM properties WHERE id = ?"
    assert to_paramstyle(sql, SQLITE) == sql


def test_question_mark_inside_a_literal_is_not_touched() -> None:
    """LIKE のパターンなどに ? が入っていても壊さない。"""
    sql = "SELECT * FROM t WHERE name LIKE 'a?b' AND id = ?"
    assert to_paramstyle(sql, POSTGRES) == (
        "SELECT * FROM t WHERE name LIKE 'a?b' AND id = %s"
    )


def test_existing_percent_is_escaped() -> None:
    """% を素通しすると psycopg がプレースホルダと誤認する。"""
    sql = "SELECT * FROM t WHERE name LIKE ? || '%'"
    assert to_paramstyle(sql, POSTGRES) == (
        "SELECT * FROM t WHERE name LIKE %s || '%%'"
    )


def test_implicit_primary_key_gets_an_identity() -> None:
    """SQLite の id INTEGER PRIMARY KEY は暗黙に採番されるが PostgreSQL は違う。"""
    ddl = "CREATE TABLE t (\n  id INTEGER PRIMARY KEY,\n  name TEXT\n)"
    out = to_postgres_ddl(ddl)
    assert "id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY" in out
    assert "name TEXT" in out


def test_other_primary_keys_are_left_alone() -> None:
    ddl = "CREATE TABLE t (version TEXT PRIMARY KEY)"
    assert to_postgres_ddl(ddl) == ddl


def test_translate_ddl_is_a_noop_for_sqlite() -> None:
    ddl = "CREATE TABLE t (id INTEGER PRIMARY KEY)"
    assert translate_ddl(ddl, SQLITE) == ddl


def test_password_is_hidden_from_logs() -> None:
    assert redact("postgresql://user:secret@host:5432/db") == (
        "postgresql://user:***@host:5432/db"
    )
    assert "secret" not in redact("postgres://user:secret@host/db")


def test_sqlite_path_survives_redaction() -> None:
    assert redact(Path("data/freming.db")) == "data/freming.db"
