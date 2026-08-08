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
    usage_type: str | None = None,
    structure: str | None = None,
    building_area: str | None = None,
    site_area: str | None = None,
    style_name: str | None = None,
    summary_en: str | None = None,
    photo_credit: str | None = None,
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
            provenance_visible = ?, scored_at = ?,
            usage_type = ?, structure = ?, building_area = ?,
            site_area = ?, style_name = ?, summary_en = ?, photo_credit = ?
        WHERE id = ?
        """,
        (
            score, score_reason, score_detail, score_model,
            summary or None, genre or None, architect or None, year_built or None,
            city or None, country or None, price or None,
            1 if provenance_visible else 0, _now(),
            usage_type or None, structure or None, building_area or None,
            site_area or None, style_name or None,
            summary_en or None, photo_credit or None,
            property_id,
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
    images = conn.execute(
        "DELETE FROM images WHERE property_id = ?", (property_id,)
    ).rowcount
    # **除外の記録も数に入れる。** images が0枚でも image_skips が残って
    # いれば、次の納品はそのURLを取りに行かない。ここで0を返すと呼び出し側は
    # 「対象なし」と報告するが、実際には消している——「失敗した」と読めて
    # 混乱する。実際に property_id=128 でこれを踏んだ。
    skips = conn.execute(
        "DELETE FROM image_skips WHERE property_id = ?", (property_id,)
    ).rowcount
    conn.commit()
    return images + skips


# 判定基準（min_short_edge_px / allowed_content_types）が変わると結論が
# 変わりうる除外理由。取得できなかった系（failed / robots / broken）は
# 基準と無関係なので、まとめて消すときも残す。
THRESHOLD_SKIP_REASONS = ("too_small", "wrong_type")


def clear_stale_skips(conn: DbConnection) -> int:
    """基準の変更で結論が変わりうる除外記録を、まとめて消す。

    一度弾いたURLは image_skips に残る限り二度と取りに行かない。これは
    相手サイトへの無駄なリクエストを避けるための設計だが、**閾値を下げても
    既存の物件が復活しない**という副作用がある。基準を変えたあとに一度
    これを呼ぶ。

    納品済みは触らない（Drive の中身と食い違う）。
    """
    marks = ",".join("?" for _ in THRESHOLD_SKIP_REASONS)
    cursor = conn.execute(
        f"DELETE FROM image_skips WHERE reason IN ({marks}) AND property_id IN "
        "(SELECT id FROM properties WHERE status != 'delivered')",
        THRESHOLD_SKIP_REASONS,
    )
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


# ----------------------------------------------------------------------
# [9] Instagram への投稿
# ----------------------------------------------------------------------
POST_PLANNED = "planned"
POST_PUBLISHING = "publishing"
POST_PUBLISHED = "published"
POST_FAILED = "failed"
POST_SKIPPED = "skipped"


def postable_properties(
    conn: DbConnection, limit: int, sources: list[str] | None = None
) -> list[Row]:
    """まだ投稿していない納品済み物件を、スコアの高い順に返す。

    納品済みに限るのは、そこまで通ったものだけが画像を持っているため。
    承認しただけで画像が用意できなかったものを投稿に回さない。
    """
    params: list = []
    where = ["p.status = 'delivered'", "po.id IS NULL"]
    if sources:
        marks = ",".join("?" for _ in sources)
        where.append(f"p.source IN ({marks})")
        params.extend(sources)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT p.* FROM properties p
        LEFT JOIN posts po ON po.property_id = p.id AND po.kind = 'feed'
        WHERE {" AND ".join(where)}
        ORDER BY p.score IS NULL, p.score DESC, p.id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()


def create_post(
    conn: DbConnection,
    kind: str,
    scheduled_at: str,
    property_id: int | None = None,
    caption: str | None = None,
    credit: str | None = None,
    parent_post_id: int | None = None,
) -> int | None:
    """予定を1件作る。同じ物件・同じ種別が既にあれば None。"""
    cursor = conn.execute(
        """
        INSERT INTO posts (
            property_id, kind, state, scheduled_at, caption, credit,
            parent_post_id, created_at
        ) VALUES (?, ?, 'planned', ?, ?, ?, ?, ?)
        ON CONFLICT (property_id, kind) DO NOTHING
        RETURNING id
        """,
        (property_id, kind, scheduled_at, caption, credit, parent_post_id, _now()),
    )
    row = cursor.fetchone()
    conn.commit()
    return row["id"] if row else None


def scheduled_posts(conn: DbConnection, until: str, states: tuple[str, ...] = ()) -> list[Row]:
    """予定表に出す行。until までの予定を時刻順に。"""
    params: list = [until]
    where = ["p.scheduled_at <= ?"]
    if states:
        marks = ",".join("?" for _ in states)
        where.append(f"p.state IN ({marks})")
        params.extend(states)
    return conn.execute(
        f"""
        SELECT p.*, pr.title, pr.location_city, pr.location_country,
               pr.source, pr.score, pr.thumbnail_url
        FROM posts p
        LEFT JOIN properties pr ON pr.id = p.property_id
        WHERE {" AND ".join(where)}
        ORDER BY p.scheduled_at, p.id
        """,
        tuple(params),
    ).fetchall()


def claim_due_post(conn: DbConnection, now: str, max_attempts: int) -> Row | None:
    """時間が来た予定を1件だけ取り、publishing にして返す。

    **取得と状態変更を1文にしてある。** 別々にすると、2つのワーカーが
    同じ行を読んで二重投稿になる。UPDATE ... WHERE state='planned' は
    先に更新できた側だけが行を返すので、あとから来た側は何も取れない。
    """
    cursor = conn.execute(
        """
        UPDATE posts SET state = 'publishing', attempts = attempts + 1
        WHERE id = (
            SELECT id FROM posts
            WHERE state = 'planned' AND scheduled_at <= ? AND attempts < ?
            ORDER BY scheduled_at, id
            LIMIT 1
        )
        RETURNING *
        """,
        (now, max_attempts),
    )
    row = cursor.fetchone()
    conn.commit()
    return row


def finish_post(conn: DbConnection, post_id: int, media_id: str, container_id: str) -> None:
    conn.execute(
        "UPDATE posts SET state = 'published', ig_media_id = ?, ig_container_id = ?, "
        "error = NULL, published_at = ? WHERE id = ?",
        (media_id, container_id, _now(), post_id),
    )
    conn.commit()


def fail_post(conn: DbConnection, post_id: int, error: str, max_attempts: int) -> str:
    """失敗を記録する。上限に達していなければ planned に戻して次回に回す。"""
    row = conn.execute("SELECT attempts FROM posts WHERE id = ?", (post_id,)).fetchone()
    attempts = row["attempts"] if row else max_attempts
    state = POST_FAILED if attempts >= max_attempts else POST_PLANNED
    conn.execute(
        "UPDATE posts SET state = ?, error = ? WHERE id = ?", (state, error[:500], post_id)
    )
    conn.commit()
    return state


def skip_post(conn: DbConnection, post_id: int) -> bool:
    """予定表から外す。投稿済みには効かない。"""
    cursor = conn.execute(
        "UPDATE posts SET state = 'skipped' WHERE id = ? AND state IN ('planned', 'failed')",
        (post_id,),
    )
    conn.commit()
    return bool(cursor.rowcount)


def abandon_post(conn: DbConnection, post_id: int) -> None:
    """取り出したあとで「出さない」と決まったものを見送りにする。

    skip_post は人が予定表から外す用で planned / failed にしか効かない。
    claim_due_post を通った行は既に publishing なので、こちらで落とす。
    """
    conn.execute("UPDATE posts SET state = 'skipped' WHERE id = ?", (post_id,))
    conn.commit()


def retry_post(conn: DbConnection, post_id: int) -> bool:
    """失敗・見送りを予定に戻す。試行回数も戻す。"""
    cursor = conn.execute(
        "UPDATE posts SET state = 'planned', attempts = 0, error = NULL "
        "WHERE id = ? AND state IN ('failed', 'skipped')",
        (post_id,),
    )
    conn.commit()
    return bool(cursor.rowcount)


def published_posts_between(conn: DbConnection, since: str, until: str) -> list[Row]:
    """期間内に公開された通常投稿。週次リールの選抜に使う。"""
    return conn.execute(
        "SELECT * FROM posts WHERE kind = 'feed' AND state = 'published' "
        "AND published_at >= ? AND published_at < ? ORDER BY published_at",
        (since, until),
    ).fetchall()


def record_reach(conn: DbConnection, post_id: int, reach: int | None) -> None:
    conn.execute(
        "UPDATE posts SET reach = ?, reach_checked_at = ? WHERE id = ?",
        (reach, _now(), post_id),
    )
    conn.commit()


def count_posts_by_state(conn: DbConnection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM posts GROUP BY state"
    ).fetchall()
    return {row["state"]: row["n"] for row in rows}


def set_permalink(conn: DbConnection, post_id: int, permalink: str | None) -> None:
    conn.execute("UPDATE posts SET permalink = ? WHERE id = ?", (permalink, post_id))
    conn.commit()


def posts_awaiting_story(conn: DbConnection, since: str, until: str) -> list[Row]:
    """期間内に公開された通常投稿。ストーリーズへ手で追加する対象。

    **ストーリーズは自動化しない**（API がリンク付きの再共有を開けていない）。
    毎日1回まとめて手で上げるので、その日の分をここで引く。
    """
    return conn.execute(
        """
        SELECT p.*, pr.title, pr.location_city, pr.location_country, pr.thumbnail_url
        FROM posts p
        LEFT JOIN properties pr ON pr.id = p.property_id
        WHERE p.kind = 'feed' AND p.state = 'published'
          AND p.published_at >= ? AND p.published_at < ?
        ORDER BY p.published_at
        """,
        (since, until),
    ).fetchall()


def mark_story_shared(conn: DbConnection, post_id: int, shared: bool) -> bool:
    """ストーリーズに追加した／取り消した、の印を付ける。"""
    cursor = conn.execute(
        "UPDATE posts SET story_shared_at = ? WHERE id = ? AND state = 'published'",
        (_now() if shared else None, post_id),
    )
    conn.commit()
    return bool(cursor.rowcount)


def undo_delivery(conn: DbConnection, property_id: int) -> str | None:
    """納品の記録を取り消して、もう一度キューに載せる。

    **Drive のフォルダは消さない。** 消せる権限をここに持ち込まない
    （納品先を壊す事故のほうが、フォルダが2つ残ることより重い）。
    人が Drive 側を片付けてからこれを呼ぶ前提で、戻り値に元のフォルダ名を
    返す。

    フォルダ名は「既存の最大値＋1」で決まるので、**再納品では番号が
    変わる**。frmg_ig006 を消して再納品しても 006 には戻らず、次の番号が
    振られる。同じ番号にしたい場合は、この関数ではなく Drive 側で
    フォルダを残したまま中身を入れ替えること。
    """
    row = conn.execute(
        "SELECT folder_name FROM deliveries WHERE property_id = ?", (property_id,)
    ).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM deliveries WHERE property_id = ?", (property_id,))
    conn.execute(
        "UPDATE properties SET status = 'approved', delivery_attempts = 0, "
        "delivery_error = NULL WHERE id = ?",
        (property_id,),
    )
    conn.commit()
    return row["folder_name"]


def properties_needing_rescore(
    conn: DbConnection, before: str | None = None, limit: int | None = None
) -> list[Row]:
    """採点し直す対象。

    足切り（築年・story）は**採点した時点のルール**で効く。あとから基準を
    変えても、既に採点済みの行は動かない。ルールを変えたら、対象を
    選んで採点し直す必要がある。

    納品済みは触らない。既に人が承認して外に出したものを、あとから
    ルールで落としても意味がない（むしろ履歴が壊れる）。
    """
    where = ["status != 'delivered'", "scored_at IS NOT NULL"]
    params: list = []
    if before:
        where.append("scored_at < ?")
        params.append(before)
    sql = (
        f"SELECT * FROM properties WHERE {' AND '.join(where)} "
        "ORDER BY scored_at ASC, id ASC"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, tuple(params)).fetchall()


def clear_scores(conn: DbConnection, ids: list[int]) -> int:
    """採点結果を消して未採点に戻す。score_pending が拾えるようにする。"""
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    cursor = conn.execute(
        f"UPDATE properties SET score = NULL, scored_at = NULL "
        f"WHERE id IN ({marks}) AND status != 'delivered'",
        tuple(ids),
    )
    conn.commit()
    return cursor.rowcount or 0


def source_outcomes(conn: DbConnection) -> list[Row]:
    """ソース別の実績。自動収集を続けるかの判断に使う。

    「何件入ったか」ではなく「**何件が承認まで行ったか**」を見る。
    たくさん入っても全部非承認なら、そのソースは費用（採点のAPI）と
    審査の手間を食っているだけになる。
    """
    return conn.execute(
        """
        SELECT source,
               COUNT(*) AS collected,
               SUM(CASE WHEN score IS NOT NULL THEN 1 ELSE 0 END) AS scored,
               SUM(CASE WHEN status = 'approved'  THEN 1 ELSE 0 END) AS approved,
               SUM(CASE WHEN status = 'rejected'  THEN 1 ELSE 0 END) AS rejected,
               SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS delivered,
               SUM(CASE WHEN status = 'pending'   THEN 1 ELSE 0 END) AS pending,
               MAX(score) AS best_score
        FROM properties
        GROUP BY source
        ORDER BY collected DESC
        """
    ).fetchall()
