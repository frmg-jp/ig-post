"""既存の物件に、記事の中の販売ページURLを後から入れる。

ストーリーズに貼るのは「その家が買えるページ」であって、記事のURLでは
ない。記事の末尾には、たいていそのリンクが書いてある。

  - Wallpaper* は記事の最後に販売ページのURLを置く
  - 6sqft は最後に Compass の掲載ページへリンクする
  - Dwell も物件によっては掲載ページを貼っている

収集時に控えるようにしたのは 2026-08-22 からで、それ以前の行には入って
いない。ここは**記事をもう一度読みに行って**、同じ抽出を当てるための経路。

## 販売サイトへは行かない

読みに行くのは**記事のページだけ**。Zillow / Redfin / Compass への
自動アクセスは行わない（規約と robots.txt の方針どおり）。記事に書いて
あるリンクをそのまま控えるだけなので、販売サイト側には1度も触らない。

物件ページそのものを収集している経路B（listing_sources）は、記事を読む
までもなく source_url が販売ページなので、そのまま写す。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from freming.collect import signals
from freming.collect.base import parse_page
from freming.config import Config
from freming.db.connection import DbConnection, Row
from freming.logging_setup import get_logger
from freming.net.client import HttpClient, RobotsDisallowed

log = get_logger(__name__)


@dataclass
class RelinkStats:
    filled: int = 0
    copied: int = 0        # 経路B。記事を読まずに source_url を写した
    not_found: int = 0     # 記事に販売ページへのリンクが無かった
    failed: int = 0        # 記事を取得できなかった
    skipped_robots: int = 0
    lines: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"販売ページを入れました {self.filled + self.copied} 件"
        parts = []
        if self.copied:
            parts.append(f"うち物件ページ由来 {self.copied}")
        if self.not_found:
            parts.append(f"記事にリンクなし {self.not_found}")
        if self.failed:
            parts.append(f"記事を取得できず {self.failed}")
        if self.skipped_robots:
            parts.append(f"robots.txt で不可 {self.skipped_robots}")
        return f"{text}（{' / '.join(parts)}）" if parts else text


def pending_rows(conn: DbConnection, limit: int | None = None) -> list[Row]:
    """販売ページが入っていない物件。**投稿に近いものから**。

    納品済み → 承認済み → それ以外の順。記事を1本ずつ取りに行くので、
    上限を付けて回せるようにしてある。
    """
    sql = (
        "SELECT id, source, source_url, title, status FROM properties "
        "WHERE (listing_url IS NULL OR listing_url = '') "
        "AND source_url IS NOT NULL "
        "ORDER BY status = 'delivered' DESC, status = 'approved' DESC, "
        "score DESC, id DESC"
    )
    params: tuple = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    return conn.execute(sql, params).fetchall()


def _save(conn: DbConnection, property_id: int, url: str) -> None:
    conn.execute(
        "UPDATE properties SET listing_url = ? WHERE id = ?", (url, property_id)
    )
    conn.commit()


def relink(
    config: Config,
    conn: DbConnection,
    limit: int | None = None,
    client: HttpClient | None = None,
) -> RelinkStats:
    """対象の記事を読み直して、販売ページのURLを入れる。"""
    stats = RelinkStats()
    rows = pending_rows(conn, limit)
    if not rows:
        log.info("販売ページを入れる対象はありません")
        return stats

    owns_client = client is None
    client = client or HttpClient(config.http)
    try:
        for row in rows:
            property_id = int(row["id"])
            title = (row["title"] or "")[:44]

            # 経路B（物件ページを直接収集）は記事を読むまでもない。
            if config.listing_source(row["source"]) is not None:
                _save(conn, property_id, row["source_url"])
                stats.copied += 1
                stats.lines.append(f"  {property_id:>5}  物件ページ  {title}")
                continue

            try:
                response = client.get(row["source_url"])
            except RobotsDisallowed:
                stats.skipped_robots += 1
                continue
            except Exception as exc:  # noqa: BLE001 - 1件の失敗で全体を止めない
                log.warning("記事を取得できませんでした: %s (%s)", row["source_url"], exc)
                stats.failed += 1
                continue

            page = parse_page(response.text, base_url=row["source_url"])
            result = signals.detect(page.text, page.links, config.for_sale_signals)
            found = signals.pick_listing_url(result.listing_links)
            if not found:
                stats.not_found += 1
                continue

            _save(conn, property_id, found)
            stats.filled += 1
            stats.lines.append(f"  {property_id:>5}  {found[:70]}")
    finally:
        if owns_client:
            client.close()

    log.info(stats.summary())
    return stats


__all__ = ["RelinkStats", "pending_rows", "relink"]
