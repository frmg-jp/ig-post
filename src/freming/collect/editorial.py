"""経路B: 編集ソース起点のクロスリファレンス。

FREMINGの独自性はここにある。編集メディアのRSSから建築記事を取得し、
本文から「販売中」を示すシグナルを検出できたものだけを候補化する。

    RSS → 記事本文の取得 → 販売シグナル検出 → 閾値以上のみ候補化

単体実行:
    python -m freming.collect.editorial --source dezeen --limit 10 [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser

from freming.collect import signals
from freming.collect.base import Candidate, normalize_url, parse_page
from freming.config import Config, EditorialSource, load_config
from freming.db.connection import DbConnection, connect
from freming.db.repository import exists_source_url, insert_candidate
from freming.logging_setup import get_logger, setup_logging
from freming.net.client import HttpClient, RobotsDisallowed

log = get_logger(__name__)


@dataclass
class Explanation:
    """1記事分の判定内訳（--explain 用）。閾値未満のものも含む。"""

    url: str
    title: str
    score: int
    text_chars: int
    from_feed_only: bool
    keywords: list[str] = field(default_factory=list)
    prices: list[str] = field(default_factory=list)
    listing_links: list[str] = field(default_factory=list)
    ignored_prices: list[str] = field(default_factory=list)
    text_head: str = ""

    def line(self) -> str:
        found: list[str] = []
        if self.keywords:
            found.append(f"kw={','.join(self.keywords[:3])}")
        if self.prices:
            found.append(f"price={','.join(self.prices[:2])}")
        if self.listing_links:
            found.append(f"listing={len(self.listing_links)}")
        if self.ignored_prices:
            found.append(f"除外price={len(self.ignored_prices)}")
        detail = " ".join(found) or "シグナルなし"
        origin = "feed" if self.from_feed_only else "記事"
        return (
            f"  {self.score}点 [{origin} {self.text_chars:>5}字] {self.title[:40]:<40} "
            f"{detail}\n        {self.url}"
        )


@dataclass
class CollectStats:
    source: str
    feed_entries: int = 0
    skipped_old: int = 0
    skipped_known: int = 0
    skipped_url_pattern: int = 0
    skipped_robots: int = 0
    fetch_failed: int = 0
    article_fetch_failed: int = 0
    used_feed_only: int = 0
    no_signal: int = 0
    inserted: int = 0
    candidates: list[str] = field(default_factory=list)
    explanations: list[Explanation] = field(default_factory=list)
    # フィードが読めなかった理由（URLごとに1つ）。「記事0件」で片付けると
    # URLの打ち間違いなのか robots で拒否されたのかが分からないため、
    # 対処の分かれる原因はここに残す。
    feed_failures: list[str] = field(default_factory=list)
    # フィードに載っていた記事URL（判定の可否に関わらず全件）。
    # url_include / url_exclude を決めるには、実際にどんなURLが来るかを
    # 見る必要がある。フィード1回のリクエストで分かる情報なので取っておく。
    entry_urls: list[str] = field(default_factory=list)
    # フィードに載っていた記事の公開日（取れたものだけ）。
    # 「10件」がフィードの窓の大きさでしかないため、それが何日分なのかを
    # 知らないと1日あたりの本数が分からない。審査に上がる件数の見積もりは
    # ここが分からないと桁で外れる。
    entry_dates: list[datetime] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 配信ペース
    # ------------------------------------------------------------------
    @property
    def window_days(self) -> float | None:
        """フィードの窓が何日分か。公開日が取れないフィードでは None。

        1件しか無い、または全件が同時刻の場合も測れないので None を返す
        （0で割らないため、というより「測れていない」を数字で塗り潰さないため）。
        """
        if len(self.entry_dates) < 2:
            return None
        span = (max(self.entry_dates) - min(self.entry_dates)).total_seconds() / 86400
        return span if span > 0 else None

    @property
    def entries_per_day(self) -> float | None:
        days = self.window_days
        if days is None or not self.entry_dates:
            return None
        # 窓の両端しか分からないので、区間の数（件数-1）で割る
        return (len(self.entry_dates) - 1) / days

    @property
    def candidates_per_week(self) -> float | None:
        """1週間あたり審査に上がる件数の見込み。

        候補率（候補/フィード件数）に配信ペースを掛ける。
        """
        pace = self.entries_per_day
        if pace is None or not self.feed_entries:
            return None
        return pace * (self.inserted / self.feed_entries) * 7

    def pace_report(self) -> str:
        """配信ペースと、そこから見た週あたりの候補数。"""
        if not self.entry_dates:
            return (
                "\n--- 配信ペース ---\n"
                "  公開日がフィードに含まれていないため測れません"
            )
        days = self.window_days
        if days is None:
            return (
                "\n--- 配信ペース ---\n"
                f"  {len(self.entry_dates)}件の公開日が同一のため測れません"
            )
        oldest = min(self.entry_dates).strftime("%Y-%m-%d")
        newest = max(self.entry_dates).strftime("%Y-%m-%d")
        lines = [
            "",
            "--- 配信ペース ---",
            f"  {len(self.entry_dates)}件が {oldest} 〜 {newest} の {days:.1f}日分"
            f"  → {self.entries_per_day:.1f} 本/日",
        ]
        weekly = self.candidates_per_week
        if weekly is not None:
            lines.append(
                f"  候補 {self.inserted}件 / {self.feed_entries}件 "
                f"→ 審査に上がるのは 週 {weekly:.1f} 件の見込み"
            )
        if len(self.entry_dates) < self.feed_entries:
            lines.append(
                f"  ※ {self.feed_entries - len(self.entry_dates)}件は公開日が取れず、"
                "この計算から除いています"
            )
        return "\n".join(lines)

    def url_pattern_report(self, top: int = 8) -> str:
        """記事URLをパスの第1階層でまとめて表示する。

        CIRCA のように物件ページとエージェント紹介ページが同じフィードに
        混ざるソースでは、ここを見れば url_exclude に書くべきパターンが
        そのまま分かる。
        """
        if not self.entry_urls:
            return ""
        from collections import Counter
        from urllib.parse import urlparse

        groups: Counter[str] = Counter()
        samples: dict[str, str] = {}
        for url in self.entry_urls:
            parts = [p for p in urlparse(url).path.split("/") if p]
            key = f"/{parts[0]}/" if len(parts) > 1 else "/（直下）"
            groups[key] += 1
            samples.setdefault(key, url)

        lines = ["", f"--- URLパターン（全 {len(self.entry_urls)} 件）---"]
        for key, count in groups.most_common(top):
            lines.append(f"  {count:>3}件  {key:<20} 例: {samples[key]}")
        if len(groups) > 1:
            lines.append(
                "  ※ 物件ページ以外が混ざっている場合は、config.yaml の "
                "url_include / url_exclude に上のパターンを指定してください"
            )
        return "\n".join(lines)

    def explain_report(self, top: int = 15) -> str:
        """判定内訳をスコア降順で表示する。フィード本文が薄いのか、
        そもそも販売物件が無いのかを切り分けるための出力。"""
        if not self.explanations:
            return ""
        ranked = sorted(self.explanations, key=lambda e: (-e.score, -e.text_chars))
        chars = [e.text_chars for e in self.explanations]
        lines = [
            "",
            f"--- 判定内訳（上位 {min(top, len(ranked))} 件 / 全 {len(ranked)} 件）---",
            *[e.line() for e in ranked[:top]],
            "",
            f"本文の長さ: 中央値 {sorted(chars)[len(chars) // 2]}字 "
            f"最小 {min(chars)}字 最大 {max(chars)}字",
        ]
        sample = ranked[0]
        lines.append(f"本文の冒頭（最高スコアの記事）: {sample.text_head}")
        return "\n".join(lines)

    def summary(self) -> str:
        line = (
            f"[{self.source}] フィード {self.feed_entries} 件 → 候補 {self.inserted} 件"
            f"（期間外 {self.skipped_old} / 取得済み {self.skipped_known} /"
            f" 対象外URL {self.skipped_url_pattern} /"
            f" robots {self.skipped_robots} / 取得失敗 {self.fetch_failed} /"
            f" シグナルなし {self.no_signal}）"
        )
        if self.used_feed_only:
            line += (
                f"\n  ※ {self.used_feed_only} 件はフィード配信分の本文で判定しました"
                f"（記事ページの取得失敗 {self.article_fetch_failed} 件）"
            )
        return line


def failure_reason(exc: Exception) -> str:
    """フィード取得の例外を、次に取るべき行動が分かる日本語にする。"""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 404:
            return "HTTP 404（このURLにフィードは無い。配信URLを確認する）"
        if status in (401, 403):
            return f"HTTP {status}（サーバーが拒否。自動取得を想定していない可能性）"
        if status == 429:
            return "HTTP 429（レート制限。時間を空けて再試行）"
        return f"HTTP {status}"
    if isinstance(exc, httpx.ConnectError):
        return "接続できない（ドメインが存在しない可能性）"
    if isinstance(exc, httpx.TimeoutException):
        return "タイムアウト"
    return f"{type(exc).__name__}: {str(exc)[:60]}"


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

    def __init__(self, config: Config, client: HttpClient, conn: DbConnection) -> None:
        self.config = config
        self.client = client
        self.conn = conn
        self._article_fetch_disabled = not config.collect.fetch_article_pages
        self._consecutive_article_failures = 0

    def _apply_source_policy(self, source: EditorialSource) -> None:
        """ソース単位の設定を反映する（記事取得の有無）。"""
        if source.fetch_article_pages is not None:
            self._article_fetch_disabled = not source.fetch_article_pages
            if not source.fetch_article_pages:
                log.info(
                    "%s: 記事ページを取得せず、フィード配信分の本文だけで判定します",
                    source.key,
                )
        self._consecutive_article_failures = 0

    def collect(
        self,
        source: EditorialSource,
        limit: int | None = None,
        dry_run: bool = False,
        explain: bool = False,
        ignore_known: bool = False,
    ) -> CollectStats:
        stats = CollectStats(source=source.key)
        self._apply_source_policy(source)
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
                self._process_entry(
                    entry, source, cutoff, stats, dry_run, explain, ignore_known
                )

        log.info(stats.summary())
        return stats

    # ------------------------------------------------------------------
    def _feed_entries(self, feed_url: str, stats: CollectStats) -> list:
        log.info("フィードを取得: %s", feed_url)
        try:
            response = self.client.get(feed_url)
        except RobotsDisallowed:
            stats.skipped_robots += 1
            stats.feed_failures.append("robots.txt が取得を許可していない")
            return []
        except Exception as exc:  # noqa: BLE001 - 1フィードの失敗で全体を止めない
            log.exception("フィードの取得に失敗: %s", feed_url)
            stats.fetch_failed += 1
            stats.feed_failures.append(failure_reason(exc))
            return []

        parsed = feedparser.parse(response.text)
        if not parsed.entries:
            # version が空なら RSS/Atom として認識できていない＝URLが違う。
            # 認識できているのに0件なら、フィードそのものが空。対処が別なので分ける。
            if not getattr(parsed, "version", ""):
                log.error(
                    "フィードを解析できませんでした: %s (%s)",
                    feed_url, getattr(parsed, "bozo_exception", None),
                )
                stats.feed_failures.append("応答はあるがフィード形式ではない（URLが違う可能性）")
            else:
                stats.feed_failures.append("フィードは読めたが記事が0件")
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
        explain: bool = False,
        ignore_known: bool = False,
    ) -> None:
        link = getattr(entry, "link", None)
        if not link:
            return
        url = normalize_url(link)
        # 除外されたURLも記録する。何が弾かれているかを見ないと、
        # url_exclude が効きすぎているのか足りないのか判断できない。
        stats.entry_urls.append(url)

        if not source.url_allowed(url):
            stats.skipped_url_pattern += 1
            log.debug("URLパターンに合わないためスキップ: %s", url)
            return

        published = _entry_published(entry)
        if published:
            # 判定の可否に関わらず記録する。配信ペースはフィードの窓全体で
            # 決まるので、ここで絞ると窓が縮んで見える。
            stats.entry_dates.append(published)
        if published and published < cutoff:
            stats.skipped_old += 1
            return

        if not ignore_known and exists_source_url(self.conn, url):
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
        if explain:
            stats.explanations.append(
                Explanation(
                    url=url,
                    title=page.title or getattr(entry, "title", "") or "",
                    score=result.score,
                    text_chars=len(page.text),
                    from_feed_only=from_feed_only,
                    keywords=list(result.keywords),
                    prices=list(result.prices),
                    listing_links=list(result.listing_links),
                    ignored_prices=list(result.ignored_prices),
                    text_head=page.text[:160],
                )
            )

        if not result.is_candidate(threshold) and not source.assume_for_sale:
            stats.no_signal += 1
            log.debug("シグナル不足（%d点 < %d点）: %s", result.score, threshold, url)
            return

        evidence = result.evidence
        if source.assume_for_sale:
            prefix = f"{source.name} は販売中の物件のみを掲載"
            evidence = f"{prefix} / {evidence}" if evidence else prefix

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
            for_sale_evidence=evidence,
            signal_score=result.score,
        )
        if insert_candidate(self.conn, candidate) is not None:
            self.conn.commit()
            stats.inserted += 1
        else:
            stats.skipped_known += 1


def collect_source(
    config: Config,
    source_key: str,
    limit: int | None = None,
    dry_run: bool = False,
    explain: bool = False,
) -> CollectStats:
    source = config.editorial_source(source_key)
    if source is None:
        raise SystemExit(f"editorial_sources に '{source_key}' がありません")
    if not source.enabled:
        raise SystemExit(f"'{source_key}' は enabled: false です。config.yaml を確認してください")

    conn = connect(config.app.target())
    try:
        with HttpClient(config.http) as client:
            return EditorialCollector(config, client, conn).collect(
                source, limit, dry_run, explain
            )
    finally:
        conn.close()


def discover_feeds(config: Config, site_url: str) -> list[tuple[str, str]]:
    """サイトのトップページから、そのサイトが公開しているフィードURLを拾う。

    フィードURLを /feed/ などの慣習から推測すると外れが多く、外れたときに
    「そのサイトにフィードが無い」のか「URLが違う」のかが区別できない。
    HTML の <link rel="alternate"> は配信側が明示している情報なので、
    ここを読めば推測せずに済む。

    戻り値は (フィードURL, ラベル) の一覧。robots.txt は HttpClient が尊重する。
    """
    from bs4 import BeautifulSoup

    with HttpClient(config.http) as client:
        response = client.get(site_url)

    soup = BeautifulSoup(response.text, "lxml")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for link in soup.find_all("link", rel=True, href=True):
        rels = link.get("rel")
        if isinstance(rels, str):
            rels = [rels]
        if "alternate" not in [r.lower() for r in rels]:
            continue
        mime = (link.get("type") or "").lower()
        if "rss" not in mime and "atom" not in mime:
            continue
        url = urljoin(str(response.url), link["href"].strip())
        if url in seen:
            continue
        seen.add(url)
        found.append((url, link.get("title") or mime))
    return found


def probe_feed(config: Config, feed_url: str, limit: int | None = None) -> CollectStats:
    """任意のフィードURLを試し、判定内訳だけを返す（DBには書き込まない）。

    config.yaml に登録する前に、そのフィードが販売物件を扱っているか、
    本文がどれくらい配信されているかを確かめるための調査用。
    """
    source = EditorialSource(
        key="probe", name=feed_url, rank="B", enabled=True, feeds=[feed_url]
    )
    probe_config = config.model_copy(deep=True)
    probe_config.collect.fetch_article_pages = False   # フィードの中身だけを見る
    probe_config.collect.lookback_days = 36500         # 期間で切らず全件を見る

    conn = connect(probe_config.app.target())
    try:
        with HttpClient(probe_config.http) as client:
            collector = EditorialCollector(probe_config, client, conn)
            # 調査用なので、取得済みかどうかに関係なくすべて表示する
            return collector.collect(
                source, limit=limit, dry_run=True, explain=True, ignore_known=True
            )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="編集ソースからの収集（経路B）")
    parser.add_argument("--source", required=True, help="editorial_sources の key（例: dezeen）")
    parser.add_argument("--limit", type=int, default=None, help="候補化する最大件数")
    parser.add_argument("--dry-run", action="store_true", help="DBに書き込まず結果だけ表示")
    parser.add_argument(
        "--explain", action="store_true", help="閾値未満も含めて判定内訳を表示（調整用）"
    )
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.app.log_dir, config.app.log_level)
    stats = collect_source(config, args.source, args.limit, args.dry_run, args.explain)
    print(stats.summary())
    for url in stats.candidates:
        print(f"  - {url}")
    if args.explain:
        print(stats.explain_report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
