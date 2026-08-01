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


def save_score(
    conn: sqlite3.Connection,
    property_id: int,
    *,
    score: float,
    score_reason: str,
    score_detail: str,
    score_model: str,
    summary: str,
    genre: str | None,
    architect: str | None,
    year_built: str | None,
    city: str | None,
    country: str | None,
    price: str | None,
    provenance_visible: bool,
) -> None:
    """スコアと、判定の過程で分かった属性を書き戻す。

    city / country / price は収集時に埋まっていることがあるので、
    LLMが空を返した場合は既存の値を残す（COALESCE ではなく、
    空文字を NULL に寄せてから既存値を優先する）。
    """
    conn.execute(
        """
        UPDATE properties SET
            score = ?, score_reason = ?, score_detail = ?, score_model = ?,
            summary = ?, genre = ?, architect = ?, year_built = ?,
            location_city    = COALESCE(location_city, ?),
            location_country = COALESCE(location_country, ?),
            price            = COALESCE(price, ?),
            provenance_visible = ?, scored_at = ?
        WHERE id = ?
        """,
        (
            score, score_reason, score_detail, score_model,
            summary or None, genre or None, architect or None, year_built or None,
            city or None, country or None, price or None,
            1 if provenance_visible else 0, _now(), property_id,
        ),
    )
    conn.commit()


def list_properties(
    conn: sqlite3.Connection,
    *,
    status: str = "pending",
    limit: int = 50,
    offset: int = 0,
    min_score: float | None = None,
    series: str | None = None,
) -> list[sqlite3.Row]:
    """審査UI用の一覧。未採点（score IS NULL）は末尾に回す。

    採点前の候補を隠すと「収集したのに出てこない」ことになるため、
    除外はせず順序だけ下げる。
    """
    # 納品済みから Drive のフォルダを開けるように、納品記録も一緒に引く
    sql = (
        "SELECT p.*, d.drive_folder_id, d.folder_name FROM properties p "
        "LEFT JOIN deliveries d ON d.property_id = p.id WHERE 1=1"
    )
    params: list = []
    if status != "all":
        sql += " AND p.status = ?"
        params.append(status)
    if min_score is not None:
        sql += " AND p.score >= ?"
        params.append(min_score)
    if series:
        sql += " AND p.series = ?"
        params.append(series)
    sql += " ORDER BY p.score IS NULL, p.score DESC, p.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return conn.execute(sql, params).fetchall()


def get_property(conn: sqlite3.Connection, property_id: int) -> sqlite3.Row | None:
    """一覧と同じ列が揃うように、納品記録も一緒に引く。"""
    return conn.execute(
        "SELECT p.*, d.drive_folder_id, d.folder_name FROM properties p "
        "LEFT JOIN deliveries d ON d.property_id = p.id WHERE p.id = ?",
        (property_id,),
    ).fetchone()


def approve_property(conn: sqlite3.Connection, property_id: int) -> bool:
    """承認する。納品済みのものは触らない（再納品を防ぐ）。"""
    cursor = conn.execute(
        "UPDATE properties SET status = 'approved', reviewed_at = ?, reject_reason = NULL "
        "WHERE id = ? AND status != 'delivered'",
        (_now(), property_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def reject_property(conn: sqlite3.Connection, property_id: int, reason: str) -> bool:
    """非承認にし、理由を feedback に残す。

    理由の蓄積が [7] 学習ループの入力になるので、理由なしの非承認は
    受け付けない（呼び出し側で検証する）。
    """
    reason = reason.strip()
    if not reason:
        raise ValueError("非承認には理由が必要です")
    cursor = conn.execute(
        "UPDATE properties SET status = 'rejected', reviewed_at = ?, reject_reason = ? "
        "WHERE id = ? AND status != 'delivered'",
        (_now(), reason, property_id),
    )
    if cursor.rowcount == 0:
        return False
    conn.execute(
        "INSERT INTO feedback (property_id, reason, created_at) VALUES (?, ?, ?)",
        (property_id, reason, _now()),
    )
    conn.commit()
    return True


def set_series(conn: sqlite3.Connection, property_id: int, series: str | None) -> bool:
    """連載企画のラベルを付け外しする。None / 空文字で解除。

    納品済みは触らない。meta.txt は納品時に書き出しているので、あとから
    ラベルだけ変えると Drive の内容と食い違う。
    """
    cursor = conn.execute(
        "UPDATE properties SET series = ? WHERE id = ? AND status != 'delivered'",
        (series or None, property_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def reset_review(conn: sqlite3.Connection, property_id: int) -> bool:
    """審査結果を取り消して pending に戻す（誤操作の復旧用）。

    feedback は消さない。人が一度そう判断した事実は学習の材料として残す。
    """
    cursor = conn.execute(
        "UPDATE properties SET status = 'pending', reviewed_at = NULL, reject_reason = NULL "
        "WHERE id = ? AND status != 'delivered'",
        (property_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def untagged_feedback(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, reason FROM feedback WHERE reason_tag IS NULL ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()


def set_feedback_tag(conn: sqlite3.Connection, feedback_id: int, tag: str) -> None:
    conn.execute("UPDATE feedback SET reason_tag = ? WHERE id = ?", (tag, feedback_id))


def tag_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """タグ別の件数（多い順）。ルール候補を出すかどうかの判断に使う。"""
    return conn.execute(
        "SELECT reason_tag, COUNT(*) AS hits FROM feedback "
        "WHERE reason_tag IS NOT NULL AND reason_tag != 'other' "
        "GROUP BY reason_tag ORDER BY hits DESC"
    ).fetchall()


def reasons_for_tag(conn: sqlite3.Connection, tag: str, limit: int = 10) -> list[str]:
    rows = conn.execute(
        "SELECT reason FROM feedback WHERE reason_tag = ? ORDER BY id DESC LIMIT ?",
        (tag, limit),
    ).fetchall()
    return [row["reason"] for row in rows]


def upsert_rule_candidate(
    conn: sqlite3.Connection, tag: str, hits: int, proposal: str
) -> bool:
    """ルール候補を登録・更新する。新規に提案したときだけ True。

    一度 dismissed にした候補は、件数が増えても提案し直さない。
    人が「これはルールにしない」と決めたものを蒸し返さないため。
    """
    existing = conn.execute(
        "SELECT state FROM rule_candidates WHERE reason_tag = ?", (tag,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO rule_candidates (reason_tag, hit_count, proposal, state, created_at) "
            "VALUES (?, ?, ?, 'proposed', ?)",
            (tag, hits, proposal, _now()),
        )
        conn.commit()
        return True
    conn.execute(
        "UPDATE rule_candidates SET hit_count = ? WHERE reason_tag = ?", (hits, tag)
    )
    conn.commit()
    return False


def list_rule_candidates(
    conn: sqlite3.Connection, state: str | None = None
) -> list[sqlite3.Row]:
    if state is None:
        return conn.execute(
            "SELECT * FROM rule_candidates ORDER BY state, hit_count DESC"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM rule_candidates WHERE state = ? ORDER BY hit_count DESC", (state,)
    ).fetchall()


def decide_rule_candidate(conn: sqlite3.Connection, tag: str, state: str) -> bool:
    """ルール候補を承認 / 却下する。自動適用はしない。"""
    if state not in ("approved", "dismissed"):
        raise ValueError("state は approved か dismissed のいずれか")
    cursor = conn.execute(
        "UPDATE rule_candidates SET state = ?, decided_at = ? WHERE reason_tag = ?",
        (state, _now(), tag),
    )
    conn.commit()
    return cursor.rowcount > 0


def approved_rules(conn: sqlite3.Connection) -> list[str]:
    """人が承認した除外ルール。スコアリングのプロンプトに載せる。"""
    rows = conn.execute(
        "SELECT proposal FROM rule_candidates WHERE state = 'approved' "
        "ORDER BY hit_count DESC"
    ).fetchall()
    return [row["proposal"] for row in rows if row["proposal"]]


def clear_images(conn: sqlite3.Connection, property_id: int) -> int:
    """取得済み画像の記録を消し、次回の納品で取り直せるようにする。

    抽出ルールを直したあとにやり直すための操作。採用しなかったURLの
    記録（image_skips）も一緒に消さないと、同じ判定が繰り返される。
    納品済みは対象外（Drive の中身と食い違うため）。
    """
    row = conn.execute(
        "SELECT status FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    if row is None or row["status"] == "delivered":
        return 0
    cursor = conn.execute("DELETE FROM images WHERE property_id = ?", (property_id,))
    conn.execute("DELETE FROM image_skips WHERE property_id = ?", (property_id,))
    conn.commit()
    return cursor.rowcount


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
