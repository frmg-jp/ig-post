"""SQLの集約。他モジュールは生SQLを書かず、ここを経由する。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from freming.collect.base import Candidate
from freming.logging_setup import get_logger

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_candidate(conn: sqlite3.Connection, candidate: Candidate) -> int | None:
    """候補を登録する。source_url が既にあれば何もせず None を返す。

    再実行しても重複登録されないことを、UNIQUE制約と INSERT OR IGNORE の
    両方で担保する。
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO properties (
            source, source_rank, source_url, title, thumbnail_url,
            content_text, for_sale_evidence, signal_score,
            price, location_city, location_country, is_for_sale,
            status, collected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            candidate.source,
            candidate.source_rank,
            candidate.source_url,
            candidate.title,
            candidate.thumbnail_url,
            candidate.content_text,
            candidate.for_sale_evidence,
            candidate.signal_score,
            candidate.price,
            candidate.location_city,
            candidate.location_country,
            candidate.is_for_sale,
            candidate.collected_at,
        ),
    )
    if cursor.rowcount == 0:
        return None
    return int(cursor.lastrowid)


def exists_source_url(conn: sqlite3.Connection, source_url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM properties WHERE source_url = ? LIMIT 1", (source_url,)
    ).fetchone()
    return row is not None


def find_by_source_url(conn: sqlite3.Connection, source_url: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM properties WHERE source_url = ?", (source_url,)
    ).fetchone()


def unscored_properties(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM properties WHERE score IS NULL AND status = 'pending' "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()


def recent_reject_reasons(conn: sqlite3.Connection, limit: int = 30) -> list[str]:
    """スコアリングのプロンプトに含める直近の非承認理由。"""
    rows = conn.execute(
        "SELECT reason FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [row["reason"] for row in rows]


def delete_properties(
    conn: sqlite3.Connection, *, source: str | None = None, property_id: int | None = None
) -> int:
    """誤って取り込んだ候補を消す。納品済みのものは対象外にする。"""
    if property_id is not None:
        cursor = conn.execute(
            "DELETE FROM properties WHERE id = ? AND status != 'delivered'", (property_id,)
        )
    elif source is not None:
        cursor = conn.execute(
            "DELETE FROM properties WHERE source = ? AND status != 'delivered'", (source,)
        )
    else:
        raise ValueError("source か property_id のどちらかを指定してください")
    return cursor.rowcount


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM properties GROUP BY status"
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}
