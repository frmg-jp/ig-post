"""SQLの集約。他モジュールは生SQLを書かず、ここを経由する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from freming.collect.base import Candidate
from freming.db.connection import DbConnection, Row
from freming.logging_setup import get_logger
from freming.values import parse_price, parse_year

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def insert_candidate(conn: DbConnection, candidate: Candidate) -> int | None:
    """候補を登録する。source_url が既にあれば何もせず None を返す。

    再実行しても重複登録されないことを、UNIQUE制約と ON CONFLICT DO NOTHING の
    両方で担保する。RETURNING を使うのは lastrowid が PostgreSQL に無いため。
    """
    cursor = conn.execute(
        """
        INSERT INTO properties (
            source, source_rank, source_url, title, thumbnail_url,
            content_text, for_sale_evidence, signal_score,
            price, location_city, location_country, is_for_sale,
            status, collected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT (source_url) DO NOTHING
        RETURNING id
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
    row = cursor.fetchone()
    if row is None:
        return None
    property_id = int(row["id"])
    refresh_values(conn, property_id)
    return property_id


def refresh_values(conn: DbConnection, property_id: int) -> None:
    """price / year_built の原文から、並べ替えと足切りに使う数値を作り直す。

    原文は表示のために残し、順序と判定はこちらを使う。price は収集時と
    採点時の二度書かれうるので、書いた側で計算せず、保存後の行を読み直して
    作る（COALESCE の結果がどちらになったかを気にしなくて済む）。
    """
    row = conn.execute(
        "SELECT price, year_built FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    if row is None:
        return
    value, currency = parse_price(row["price"])
    conn.execute(
        "UPDATE properties SET price_value = ?, price_currency = ?, year_built_value = ? "
        "WHERE id = ?",
        (value, currency, parse_year(row["year_built"]), property_id),
    )


def backfill_values(conn: DbConnection) -> int:
    """既存の行に数値列を埋める。マイグレーション後に一度だけ実行する。

    SQL だけでは書式を解けない（"$1,250,000" / "3,980 萬" / "built in 1902"）
    ので、Python 側で読み直して書く。
    """
    ids = [r["id"] for r in conn.execute("SELECT id FROM properties ORDER BY id").fetchall()]
    for property_id in ids:
        refresh_values(conn, property_id)
    conn.commit()
    return len(ids)


def exists_source_url(conn: DbConnection, source_url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM properties WHERE source_url = ? LIMIT 1", (source_url,)
    ).fetchone()
    return row is not None


def find_by_source_url(conn: DbConnection, source_url: str) -> Row | None:
    return conn.execute(
        "SELECT * FROM properties WHERE source_url = ?", (source_url,)
    ).fetchone()


def unscored_properties(conn: DbConnection, limit: int = 50) -> list[Row]:
    return conn.execute(
        "SELECT * FROM properties WHERE score IS NULL AND status = 'pending' "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()


def recent_reject_reasons(conn: DbConnection, limit: int = 30) -> list[str]:
    """スコアリングのプロンプトに含める直近の非承認理由。"""
    rows = conn.execute(
        "SELECT reason FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [row["reason"] for row in rows]


def save_score(
    conn: DbConnection,
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
    # 採点で year_built が入り、price が上書きされることがある。原文が
    # 変わったら数値も作り直す。
    refresh_values(conn, property_id)
    conn.commit()


# 審査UIの並べ替え。値が無い行（価格不明・築年不明・未採点）は、どの順序でも
# 末尾に回す。先頭に固まると、順序を変えるたびに同じ「分からない」行を
# 読まされることになるため。
SORTS: dict[str, str] = {
    "score": "p.score IS NULL, p.score DESC, p.id DESC",
    "newest": "p.collected_at DESC, p.id DESC",
    "oldest": "p.collected_at ASC, p.id ASC",
    "price_desc": "p.price_value IS NULL, p.price_value DESC, p.id DESC",
    "price_asc": "p.price_value IS NULL, p.price_value ASC, p.id DESC",
    "built_oldest": "p.year_built_value IS NULL, p.year_built_value ASC, p.id DESC",
}
DEFAULT_SORT = "score"
_PRICE_SORTS = {"price_desc": "DESC", "price_asc": "ASC"}


def price_jpy_expr(rates: dict[str, float]) -> str:
    """price_value を円に直すSQL式を組み立てる。

    **DBには換算値を保存しない。** 保存するとレートを直すたびに全件を
    書き直すことになる。ここで毎回計算すれば、config.yaml のレートを
    変えた次の表示から順序に反映される。

    レートを持たない通貨（と通貨が判別できなかった行）は NULL になり、
    価格順では末尾に回る。金額だけで比べると桁が違うので、換算できない
    ものを混ぜるくらいなら並べない。

    数値は float として config から来る（pydantic が検証済み）。SQLに
    直接埋めるのは、ORDER BY にプレースホルダを置くと方言差が出るため。
    """
    if not rates:
        return "p.price_value"
    whens = " ".join(
        f"WHEN '{code}' THEN {float(rate)!r}"
        for code, rate in sorted(rates.items())
        if code.isalpha()
    )
    return f"(p.price_value * CASE p.price_currency {whens} ELSE NULL END)"


def list_properties(
    conn: DbConnection,
    *,
    status: str = "pending",
    limit: int = 50,
    offset: int = 0,
    min_score: float | None = None,
    series: str | None = None,
    sort: str = DEFAULT_SORT,
    fx_rates: dict[str, float] | None = None,
) -> list[Row]:
    """審査UI用の一覧。未採点（score IS NULL）は末尾に回す。

    採点前の候補を隠すと「収集したのに出てこない」ことになるため、
    除外はせず順序だけ下げる。

    sort は SORTS のキーのみ受け付ける。ここに文字列を直接埋めるので、
    未知の値は既定に落として SQL に渡さない。

    fx_rates を渡すと、価格順は円に換算した値で並べる。渡さなければ
    原文の通貨のままの数値で並ぶ（通貨をまたぐと意味を成さない）。
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
    order = SORTS.get(sort, SORTS[DEFAULT_SORT])
    if fx_rates and sort in _PRICE_SORTS:
        # 通貨をまたいで比べるため、円に直した値で並べる。換算できない行は
        # NULL になるので、値が無い行と同じく末尾に回る。
        expr = price_jpy_expr(fx_rates)
        order = f"{expr} IS NULL, {expr} {_PRICE_SORTS[sort]}, p.id DESC"
    sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
    params += [limit, offset]
    return conn.execute(sql, params).fetchall()


def get_property(conn: DbConnection, property_id: int) -> Row | None:
    """一覧と同じ列が揃うように、納品記録も一緒に引く。"""
    return conn.execute(
        "SELECT p.*, d.drive_folder_id, d.folder_name FROM properties p "
        "LEFT JOIN deliveries d ON d.property_id = p.id WHERE p.id = ?",
        (property_id,),
    ).fetchone()


def approve_property(conn: DbConnection, property_id: int) -> bool:
    """承認する。納品済みのものは触らない（再納品を防ぐ）。

    納品の試行記録も消す。一度失敗した候補を審査UIで承認し直したときに、
    上限に達したままだと自動納品に拾われず、何も起きないように見えるため。
    """
    cursor = conn.execute(
        "UPDATE properties SET status = 'approved', reviewed_at = ?, reject_reason = NULL, "
        "delivery_attempts = 0, delivery_error = NULL, delivery_attempted_at = NULL "
        "WHERE id = ? AND status != 'delivered'",
        (_now(), property_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def reject_property(conn: DbConnection, property_id: int, reason: str) -> bool:
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


def set_series(conn: DbConnection, property_id: int, series: str | None) -> bool:
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


def reset_review(conn: DbConnection, property_id: int) -> bool:
    """審査結果を取り消して pending に戻す（誤操作の復旧用）。

    feedback は消さない。人が一度そう判断した事実は学習の材料として残す。
    """
    cursor = conn.execute(
        "UPDATE properties SET status = 'pending', reviewed_at = NULL, reject_reason = NULL, "
        "delivery_attempts = 0, delivery_error = NULL, delivery_attempted_at = NULL "
        "WHERE id = ? AND status != 'delivered'",
        (property_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


# ----------------------------------------------------------------------
# 自動納品のキュー
# ----------------------------------------------------------------------
def delivery_queue(
    conn: DbConnection,
    *,
    limit: int,
    max_attempts: int,
    retry_after_sec: float,
) -> list[Row]:
    """自動納品が次に処理すべき承認済み候補を返す。

    除外するもの:
      - 試行回数が上限に達したもの（自動では諦め、審査UIから人が再試行する）
      - 直前の失敗から retry_after_sec が経っていないもの

    未試行を先に、次に古い試行から処理する。新しく承認したものが
    失敗続きの候補の後ろで待たされないようにするため。
    """
    # 待ち時間の判定は Python 側で刻む。SQL の datetime('now', ...) は
    # SQLite にしか無く、書式も _now() の ISO と食い違う（比較が常に偽になる）。
    cutoff = (
        datetime.now(UTC) - timedelta(seconds=max(retry_after_sec, 0))
    ).isoformat()
    return conn.execute(
        "SELECT * FROM properties WHERE status = 'approved' "
        "AND delivery_attempts < ? "
        "AND (delivery_attempted_at IS NULL OR delivery_attempted_at <= ?) "
        "ORDER BY delivery_attempts, "
        "         CASE WHEN delivery_attempted_at IS NULL THEN 0 ELSE 1 END, "
        "         delivery_attempted_at, score DESC, id "
        "LIMIT ?",
        (max_attempts, cutoff, limit),
    ).fetchall()


def record_delivery_failure(
    conn: DbConnection, property_id: int, message: str
) -> int:
    """納品の失敗を記録し、その時点の試行回数を返す。

    status は approved のまま置く。失敗を別ステータスにすると
    「承認したのに一覧から消えた」ことになり、追跡できなくなる。
    """
    # 時刻は必ず _now() の ISO で入れる。delivery_queue が同じ形式の
    # カットオフと文字列比較するため、書式を1つに揃える必要がある。
    conn.execute(
        "UPDATE properties SET delivery_attempts = delivery_attempts + 1, "
        "delivery_error = ?, delivery_attempted_at = ? WHERE id = ?",
        (message[:500], _now(), property_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT delivery_attempts FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    return int(row["delivery_attempts"]) if row else 0


def retry_delivery(conn: DbConnection, property_id: int) -> bool:
    """試行回数を戻して、自動納品の対象に復帰させる（審査UIの再試行）。"""
    cursor = conn.execute(
        "UPDATE properties SET delivery_attempts = 0, delivery_error = NULL, "
        "delivery_attempted_at = NULL WHERE id = ? AND status = 'approved'",
        (property_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def delivery_queue_size(conn: DbConnection, max_attempts: int) -> int:
    """まだ自動納品に拾われる見込みのある件数（待ち時間中のものも含む）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM properties WHERE status = 'approved' "
        "AND delivery_attempts < ?",
        (max_attempts,),
    ).fetchone()
    return int(row["n"]) if row else 0


def untagged_feedback(conn: DbConnection, limit: int = 10) -> list[Row]:
    return conn.execute(
        "SELECT id, reason FROM feedback WHERE reason_tag IS NULL ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()


def set_feedback_tag(conn: DbConnection, feedback_id: int, tag: str) -> None:
    conn.execute("UPDATE feedback SET reason_tag = ? WHERE id = ?", (tag, feedback_id))


def tag_counts(conn: DbConnection) -> list[Row]:
    """タグ別の件数（多い順）。ルール候補を出すかどうかの判断に使う。"""
    return conn.execute(
        "SELECT reason_tag, COUNT(*) AS hits FROM feedback "
        "WHERE reason_tag IS NOT NULL AND reason_tag != 'other' "
        "GROUP BY reason_tag ORDER BY hits DESC"
    ).fetchall()


def reasons_for_tag(conn: DbConnection, tag: str, limit: int = 10) -> list[str]:
    rows = conn.execute(
        "SELECT reason FROM feedback WHERE reason_tag = ? ORDER BY id DESC LIMIT ?",
        (tag, limit),
    ).fetchall()
    return [row["reason"] for row in rows]


def upsert_rule_candidate(
    conn: DbConnection, tag: str, hits: int, proposal: str
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
    conn: DbConnection, state: str | None = None
) -> list[Row]:
    if state is None:
        return conn.execute(
            "SELECT * FROM rule_candidates ORDER BY state, hit_count DESC"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM rule_candidates WHERE state = ? ORDER BY hit_count DESC", (state,)
    ).fetchall()


def decide_rule_candidate(conn: DbConnection, tag: str, state: str) -> bool:
    """ルール候補を承認 / 却下する。自動適用はしない。"""
    if state not in ("approved", "dismissed"):
        raise ValueError("state は approved か dismissed のいずれか")
    cursor = conn.execute(
        "UPDATE rule_candidates SET state = ?, decided_at = ? WHERE reason_tag = ?",
        (state, _now(), tag),
    )
    conn.commit()
    return cursor.rowcount > 0


def approved_rules(conn: DbConnection) -> list[str]:
    """人が承認した除外ルール。スコアリングのプロンプトに載せる。"""
    rows = conn.execute(
        "SELECT proposal FROM rule_candidates WHERE state = 'approved' "
        "ORDER BY hit_count DESC"
    ).fetchall()
    return [row["proposal"] for row in rows if row["proposal"]]


def clear_images(conn: DbConnection, property_id: int) -> int:
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


def deletable_properties(
    conn: DbConnection,
    *,
    source: str | None = None,
    property_id: int | None = None,
    ids: list[int] | None = None,
) -> list[Row]:
    """delete_properties が消す対象を、消す前に一覧で返す。

    本番DBに対する破壊的操作なので、実行前に何が消えるか目で見られる
    ようにする（--dry-run）。条件は delete_properties と同じものを使う。
    """
    if property_id is not None:
        return conn.execute(
            "SELECT * FROM properties WHERE id = ? AND status != 'delivered'",
            (property_id,),
        ).fetchall()
    if ids is not None:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        return conn.execute(
            f"SELECT * FROM properties WHERE id IN ({marks}) AND status != 'delivered'",
            tuple(ids),
        ).fetchall()
    if source is not None:
        return conn.execute(
            "SELECT * FROM properties WHERE source = ? AND status != 'delivered'",
            (source,),
        ).fetchall()
    raise ValueError("source / property_id / ids のいずれかを指定してください")


def delete_properties(
    conn: DbConnection,
    *,
    source: str | None = None,
    property_id: int | None = None,
    ids: list[int] | None = None,
) -> int:
    """誤って取り込んだ候補を消す。納品済みのものは対象外にする。"""
    if property_id is not None:
        cursor = conn.execute(
            "DELETE FROM properties WHERE id = ? AND status != 'delivered'", (property_id,)
        )
    elif ids is not None:
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        cursor = conn.execute(
            f"DELETE FROM properties WHERE id IN ({marks}) AND status != 'delivered'",
            tuple(ids),
        )
    elif source is not None:
        cursor = conn.execute(
            "DELETE FROM properties WHERE source = ? AND status != 'delivered'", (source,)
        )
    else:
        raise ValueError("source / property_id / ids のいずれかを指定してください")
    return cursor.rowcount


def properties_for_photo_audit(conn: DbConnection) -> list[Row]:
    """写真を検査する対象。納品済みは触らない。"""
    return conn.execute(
        "SELECT id, source, source_url, title, thumbnail_url FROM properties "
        "WHERE status != 'delivered' ORDER BY id"
    ).fetchall()


def count_by_status(conn: DbConnection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM properties GROUP BY status"
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}
