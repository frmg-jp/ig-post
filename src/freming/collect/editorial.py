"""経路B: 編集ソース起点のクロスリファレンス。

FREMINGの独自性はここにある。編集メディアのRSSから建築記事を取得し、
本文から「販売中」を示すシグナルを検出できたものだけを候補化する。

    RSS → 記事本文の取得 → 販売シグナル検出 → 閾値以上のみ候補化

単体実行:
    python -m freming.collect.editorial --source dezeen --limit 10 [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import feedparser

from freming.collect import signals
from freming.collect.base import Candidate, normalize_url, parse_page
from freming.config import Config, EditorialSource, load_config
from freming.db.connection import connect
from freming.db.repository import exists_source_url, insert_candidate
from freming.logging_setup import get_logger, setup_logging
from freming.net.client import HttpClient, RobotsDisallowed

log = get_logger(__name__)


@dataclass
class CollectStats:
    source: str
    feed_entries: int = 0
    skipped_old: int = 0
    skipped_known: int = 0
    skipped_robots: int = 0
    fetch_failed: int = 0
    article_fetch_failed: int = 0
    used_feed_only: int = 0
    no_signal: int = 0
    inserted: int = 0
    candidates: list[str] = field(default_factory=list)

    def summary(self) -> str:
        line = (
            f"[{self.source}] フィード {self.feed_entries} 件 → 候補 {self.inserted} 件"
            f"（期間外 {self.skipped_old} / 取得済み {self.skipped_known} /"
            f" robots {self.skipped_robots} / 取得失敗 {self.fetch_failed} /"
            f" シグナルなし {self.no_signal}）"
        )
        if self.used_feed_only:
            line += (
                f"\n  ※ {self.used_feed_only} 件はフィード配信分の本文で判定しました"
                f"（記事ページの取得失敗 {self.article_fetch_failed} 件）"
            )
        return line


def _entry_published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return None


def _entry_html(entry) -> str:
    """フィードが配信している本文（要約・全文）を集めて1つのHTMLにする。

    記事ページを取りに行かなくても判定できるだけの材料が、たいていは
    フィード側に含まれている。
    """
    parts: list[str] = []
    title = getattr(entry, "title", None)
    if title:
        parts.append(f"<h1>{title}</h1>")
    summary = getattr(entry, "summary", None)
    if summary:
        parts.append(summary)
    for item in getattr(entry, "content", None) or []:
        value = item.get("value") if isinstance(item, dict) else getattr(item, "value", None)
        if value:
            parts.append(value)
    return "".join(parts)


class EditorialCollector:
    """編集ソース1つ分の収集。"""

    def __init__(self, config: Config, client: HttpClient, conn: sqlite3.Connection) -> None:
        self.config = config
        self.client = client
        self.conn = conn
        self._article_fetch_disabled = not config.collect.fetch_article_pages
        self._consecutive_article_failures = 0

    def collect(
        self, source: EditorialSource, limit: int | None = None, dry_run: bool = False
    ) -> CollectStats:
        stats = CollectStats(source=source.key)
        if not source.feeds:
            log.warning(
                "%s: フィードURLが未設定です。config.yaml の editorial_sources に "
                "公式RSSのURLを設定してください。", source.key
            )
            return stats

        max_items = limit or self.config.collect.max_items_per_source_per_run
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.collect.lookback_days)

        for feed_url in source.feeds:
            if stats.inserted >= max_items:
                break
            for entry in self._feed_entries(feed_url, stats):
                if stats.inserted >= max_items:
                    break
                self._process_entry(entry, source, cutoff, stats, dry_run)

        log.info(stats.summary())
        return stats

    # ------------------------------------------------------------------
    def _feed_entries(self, feed_url: str, stats: CollectStats) -> list:
        log.info("フィードを取得: %s", feed_url)
        try:
            response = self.client.get(feed_url)
        except RobotsDisallowed:
            stats.skipped_robots += 1
            return []
        except Exception:  # noqa: BLE001 - 1フィードの失敗で全体を止めない
            log.exception("フィードの取得に失敗: %s", feed_url)
            stats.fetch_failed += 1
            return []

        parsed = feedparser.parse(response.text)
        if parsed.bozo and not parsed.entries:
            log.error("フィードを解析できませんでした: %s (%s)", feed_url, parsed.bozo_exception)
            return []
        stats.feed_entries += len(parsed.entries)
        return parsed.entries

    def _note_article_failure(self, url: str, exc: Exception, stats: CollectStats) -> None:
        """記事ページの取得失敗を記録し、続くようなら取得自体をやめる。

        ブラウザ以外からのアクセスを拒否するサイトでは全記事が失敗する。
        User-Agent を偽装して回避することはしないので、フィード配信分の
        本文で判定を続け、無駄なリクエストを送り続けないようにする。
        """
        stats.article_fetch_failed += 1
        self._consecutive_article_failures += 1
        limit = self.config.collect.article_fetch_failure_limit

        if self._consecutive_article_failures == 1:
            log.warning("記事ページを取得できませんでした: %s (%s)", url, exc)
        else:
            log.debug("記事ページを取得できませんでした: %s (%s)", url, exc)

        if not self._article_fetch_disabled and self._consecutive_article_failures >= limit:
            self._article_fetch_disabled = True
            log.warning(
                "記事ページの取得が %d 回続けて失敗したため、この実行では"
                "フィード配信分の本文だけで判定します。"
                "（サイト側がブラウザ以外を拒否している可能性があります。"
                "User-Agent の偽装による回避は行いません）",
                limit,
            )

    def _process_entry(
        self,
        entry,
        source: EditorialSource,
        cutoff: datetime,
        stats: CollectStats,
        dry_run: bool,
    ) -> None:
        link = getattr(entry, "link", None)
        if not link:
            return
        url = normalize_url(link)

        published = _entry_published(entry)
        if published and published < cutoff:
            stats.skipped_old += 1
            return

        if exists_source_url(self.conn, url):
            stats.skipped_known += 1
            log.debug("取得済みのためスキップ: %s", url)
            return

        # まずフィードが配信している本文で判定材料を作る
        page = parse_page(_entry_html(entry), base_url=url)
        from_feed_only = True

        if not self._article_fetch_disabled:
            try:
                response = self.client.get(url)
            except RobotsDisallowed:
                stats.skipped_robots += 1
                return
            except Exception as exc:  # noqa: BLE001 - 1記事の失敗で全体を止めない
                self._note_article_failure(url, exc, stats)
            else:
                page = parse_page(response.text, base_url=url)
                from_feed_only = False
                self._consecutive_article_failures = 0

        if from_feed_only:
            stats.used_feed_only += 1

        if not page.text.strip():
            log.warning("本文を取得できませんでした（フィードにも本文なし）: %s", url)
            stats.fetch_failed += 1
            return

        result = signals.detect(page.text, page.links, self.config.for_sale_signals)

        threshold = self.config.for_sale_signals.min_signal_score
        if not result.is_candidate(threshold):
            stats.no_signal += 1
            log.debug("シグナル不足（%d点 < %d点）: %s", result.score, threshold, url)
            return

        title = page.title or getattr(entry, "title", None)
        log.info("候補: [%d点] %s — %s", result.score, title, url)
        stats.candidates.append(url)

        if dry_run:
            stats.inserted += 1
            return

        candidate = Candidate(
            source=source.key,
            source_rank=source.rank,
            source_url=url,
            title=title,
            thumbnail_url=page.thumbnail_url,
            content_text=page.text,
            for_sale_evidence=result.evidence,
            signal_score=result.score,
        )
        if insert_candidate(self.conn, candidate) is not None:
            self.conn.commit()
            stats.inserted += 1
        else:
            stats.skipped_known += 1


def collect_source(
    config: Config, source_key: str, limit: int | None = None, dry_run: bool = False
) -> CollectStats:
    source = config.editorial_source(source_key)
    if source is None:
        raise SystemExit(f"editorial_sources に '{source_key}' がありません")
    if not source.enabled:
        raise SystemExit(f"'{source_key}' は enabled: false です。config.yaml を確認してください")

    conn = connect(config.app.db_path)
    try:
        with HttpClient(config.http) as client:
            return EditorialCollector(config, client, conn).collect(source, limit, dry_run)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="編集ソースからの収集（経路B）")
    parser.add_argument("--source", required=True, help="editorial_sources の key（例: dezeen）")
    parser.add_argument("--limit", type=int, default=None, help="候補化する最大件数")
    parser.add_argument("--dry-run", action="store_true", help="DBに書き込まず結果だけ表示")
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.app.log_dir, config.app.log_level)
    stats = collect_source(config, args.source, args.limit, args.dry_run)
    print(stats.summary())
    for url in stats.candidates:
        print(f"  - {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
