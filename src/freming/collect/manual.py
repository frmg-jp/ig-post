"""手動URL投入。

Zillow / Redfin / Compass は利用規約で自動収集を禁止しているため、
自動クロールは実装せず、人がURLを貼った1件だけを取得する経路のみを用意する。
robots.txt の判定はここでも同じように適用する。

単体実行:
    python -m freming.collect.manual <URL>
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from urllib.parse import urlparse

from freming.collect import signals
from freming.collect.base import Candidate, normalize_url, parse_page
from freming.config import Config, load_config
from freming.db.connection import connect
from freming.db.repository import find_by_source_url, insert_candidate
from freming.logging_setup import get_logger, setup_logging
from freming.net.client import HttpClient, RobotsDisallowed

log = get_logger(__name__)


class AlreadyCollected(RuntimeError):
    """同じURLが既に登録されている。"""

    def __init__(self, url: str, property_id: int) -> None:
        super().__init__(f"このURLは登録済みです（id={property_id}）: {url}")
        self.property_id = property_id


def guess_source(config: Config, url: str) -> tuple[str, str | None]:
    """URLのドメインから既知のソースを推定する。

    Returns:
        (source_key, source_rank)
    """
    netloc = (urlparse(url).netloc or "").lower().removeprefix("www.")
    for source in config.listing_sources + config.editorial_sources:
        base = getattr(source, "base_url", None)
        if not base:
            continue
        base_netloc = (urlparse(base).netloc or "").lower().removeprefix("www.")
        if base_netloc and (netloc == base_netloc or netloc.endswith("." + base_netloc)):
            return source.key, source.rank
    return "manual", None


def ingest_url(config: Config, url: str, conn: sqlite3.Connection | None = None) -> int:
    """URLを1件だけ取得して候補化し、property_id を返す。"""
    url = normalize_url(url)
    owns_conn = conn is None
    conn = conn or connect(config.app.db_path)
    try:
        existing = find_by_source_url(conn, url)
        if existing is not None:
            raise AlreadyCollected(url, existing["id"])

        with HttpClient(config.http) as client:
            response = client.get(url)

        page = parse_page(response.text, base_url=url)
        result = signals.detect(page.text, page.links, config.for_sale_signals)
        source_key, source_rank = guess_source(config, url)

        # 手動投入は人が選んだものなので、シグナル不足でも候補化する。
        # 販売可否の最終判断は [2] スコアリングに委ねる。
        if not result.is_candidate(config.for_sale_signals.min_signal_score):
            log.info("販売シグナルは弱い（%d点）が、手動投入のため候補化します", result.score)

        candidate = Candidate(
            source=source_key,
            source_rank=source_rank,
            source_url=url,
            title=page.title,
            thumbnail_url=page.thumbnail_url,
            content_text=page.text,
            for_sale_evidence=result.evidence or "手動投入",
            signal_score=result.score,
        )
        property_id = insert_candidate(conn, candidate)
        if property_id is None:  # 競合で先に入った場合
            row = find_by_source_url(conn, url)
            raise AlreadyCollected(url, row["id"])
        conn.commit()
        log.info("候補化しました: id=%d %s", property_id, url)
        return property_id
    finally:
        if owns_conn:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="URLを1件だけ取得して候補化する")
    parser.add_argument("url")
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.app.log_dir, config.app.log_level)
    try:
        property_id = ingest_url(config, args.url)
    except AlreadyCollected as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RobotsDisallowed as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OK: property_id={property_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
