"""DB接続のヘルパ。SQLite と PostgreSQL のどちらにも繋ぐ。

呼び出し側は接続の実体を意識しない。`conn.execute(sql, params)` が
カーソルを返し、行は `row["column"]` で読める、という形に揃えてある。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

from freming.db.dialect import POSTGRES, SQLITE, dialect_of, redact, to_paramstyle
from freming.logging_setup import get_logger

log = get_logger(__name__)


@runtime_checkable
class Cursor(Protocol):
    rowcount: int

    def fetchone(self) -> Any: ...
    def fetchall(self) -> list[Any]: ...


@runtime_checkable
class DbConnection(Protocol):
    """repository が要求する最小の接続インターフェース。

    sqlite3.Connection がそのまま満たすので、SQLite 側にラッパは要らない。
    """

    def execute(self, sql: str, params: Any = ..., /) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


# 行の型。sqlite3.Row も psycopg の dict_row も row["key"] で読める。
Row = Any


class PostgresConnection:
    """psycopg 接続を sqlite3.Connection と同じ触り心地にする薄いラッパ。

    やっていることは2つだけ。
      - ? を %s に変換する（SQLはSQLiteの書き方で統一しているため）
      - execute がカーソルを返すようにする
    """

    def __init__(self, dsn: str) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)

    def execute(self, sql: str, params: Any = ()) -> Any:
        cursor = self._conn.cursor()
        cursor.execute(to_paramstyle(sql, POSTGRES), tuple(params))
        return cursor

    def executescript(self, sql: str) -> None:
        """複数文をまとめて実行する（マイグレーション用）。"""
        with self._conn.cursor() as cursor:
            cursor.execute(sql)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> PostgresConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def connect(target: str | Path) -> DbConnection:
    """接続先の形から SQLite / PostgreSQL を選ぶ。

    target がパスなら SQLite、postgresql:// なら PostgreSQL。
    """
    if dialect_of(target) == POSTGRES:
        log.debug("PostgreSQL に接続します: %s", redact(target))
        return PostgresConnection(str(target))

    db_path = Path(target)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def dialect_for(target: str | Path) -> str:
    return dialect_of(target)


@contextmanager
def session(target: str | Path) -> Iterator[DbConnection]:
    """with ブロックを抜けるときに commit / 例外時 rollback する接続。"""
    conn = connect(target)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


__all__ = [
    "DbConnection",
    "PostgresConnection",
    "Row",
    "SQLITE",
    "POSTGRES",
    "connect",
    "dialect_for",
    "session",
]
