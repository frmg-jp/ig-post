"""SQLite 接続のヘルパ。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect(db_path: str | Path) -> sqlite3.Connection:
    """外部キー・WAL を有効にした接続を返す。行は dict 風に読める。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """with ブロックを抜けるときに commit / 例外時 rollback する接続。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
