"""経路B（編集ソース起点のクロスリファレンス）の統合テスト。

HTTP層を差し替えて、RSS → 記事取得 → 販売シグナル判定 → 候補化 までを
ネットワークなしで通す。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from freming.collect.editorial import EditorialCollector
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.net.client import RobotsDisallowed

FEED_URL = "https://www.dezeen.com/architecture/feed/"


def _rfc822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def _feed(entries: list[tuple[str, str, datetime]], description: str = "") -> str:
    body = f"<description><![CDATA[{description}]]></description>" if description else ""
    items = "".join(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>{_rfc822(published)}</pubDate>{body}</item>"
        for title, link, published in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<rss version=\"2.0\"><channel><title>Dezeen</title>{items}</channel></rss>"
    )


def _article(body: str, links: str = "") -> str:
    return (
        "<html><head><meta property='og:title' content='Article'>"
        "<meta property='og:image' content='/hero.jpg'></head>"
        f"<body><p>{body}</p>{links}</body></html>"
    )


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200


class Forbidden(RuntimeError):
    """記事ページが 403 を返す状況（ブラウザ以外を拒否するサイト）。"""


class FakeClient:
    """HttpClient の代わり。robots.txt もレート制限も既に検証済みなので省く。"""

    def __init__(
        self,
        pages: dict[str, str],
        disallowed: set[str] | None = None,
        forbidden: set[str] | None = None,
    ) -> None:
        self.pages = pages
        self.disallowed = disallowed or set()
        self.forbidden = forbidden or set()
        self.requested: list[str] = []

    def get(self, url: str, **_kwargs) -> _Response:
        self.requested.append(url)
        if url in self.disallowed:
            raise RobotsDisallowed(url)
        if url in self.forbidden:
            raise Forbidden(f"403 Forbidden for url '{url}'")
        if url not in self.pages:
            raise RuntimeError(f"想定外のURL: {url}")
        return _Response(self.pages[url])


@pytest.fixture()
def config():
    return load_config("config.yaml")


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    connection = connect(path)
    yield connection
    connection.close()


@pytest.fixture()
def source(config):
    """編集ソース1つ分。フィードURLはテスト用に固定し、config.yaml の
    実際のURL変更でテストが壊れないようにする。"""
    src = config.editorial_source("dezeen").model_copy(deep=True)
    src.feeds = [FEED_URL]
    return src


@pytest.fixture()
def listing_source(config):
    """販売専門メディア（掲載＝売出中）。"""
    src = config.editorial_source("dezeen").model_copy(deep=True)
    src.key = "wowhaus"
    src.name = "WowHaus"
    src.rank = "A"
    src.assume_for_sale = True
    src.feeds = [FEED_URL]
    return src


def _run(config, conn, pages, source, disallowed=None, limit=None, dry_run=False,
         forbidden=None, explain=False):
    client = FakeClient(pages, disallowed, forbidden)
    collector = EditorialCollector(config, client, conn)
    return collector.collect(source, limit=limit, dry_run=dry_run, explain=explain), client


def test_only_articles_with_for_sale_signals_become_candidates(config, conn, source) -> None:
    now = datetime.now(timezone.utc)
    pages = {
        FEED_URL: _feed(
            [
                ("For sale loft", "https://www.dezeen.com/a/", now - timedelta(days=1)),
                ("Just a house", "https://www.dezeen.com/b/", now - timedelta(days=2)),
            ]
        ),
        "https://www.dezeen.com/a": _article(
            "The converted firehouse is now for sale with an asking price of $2.4 million."
        ),
        "https://www.dezeen.com/b": _article(
            "The studio completed the house in 2019. Photography is by someone."
        ),
    }

    stats, _ = _run(config, conn, pages, source)

    assert stats.inserted == 1
    assert stats.no_signal == 1
    rows = conn.execute("SELECT * FROM properties").fetchall()
    assert len(rows) == 1
    assert rows[0]["source_url"] == "https://www.dezeen.com/a"
    assert rows[0]["source"] == "dezeen"
    assert rows[0]["source_rank"] == "S"
    assert rows[0]["status"] == "pending"
    assert "asking price" in rows[0]["for_sale_evidence"].lower()
    assert rows[0]["content_text"]          # スコアリング用に本文を保持している
    assert rows[0]["thumbnail_url"] == "https://www.dezeen.com/hero.jpg"


def test_editorial_article_linking_to_a_listing_site_is_picked_up(config, conn, source) -> None:
    """「編集メディア掲載 かつ 販売中」を拾えること。これが Hidden Gem の正体。"""
    now = datetime.now(timezone.utc)
    pages = {
        FEED_URL: _feed([("Loft", "https://www.dezeen.com/c/", now)]),
        "https://www.dezeen.com/c": _article(
            "A rare SOMA loft.",
            links='<a href="https://www.sothebysrealty.com/id/999">listing</a>',
        ),
    }

    stats, _ = _run(config, conn, pages, source)

    assert stats.inserted == 1
    row = conn.execute("SELECT * FROM properties").fetchone()
    assert "sothebysrealty.com" in row["for_sale_evidence"]
    assert row["signal_score"] >= config.for_sale_signals.min_signal_score


def test_articles_older_than_lookback_are_skipped(config, conn, source) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=config.collect.lookback_days + 5)
    pages = {FEED_URL: _feed([("Old", "https://www.dezeen.com/old/", old)])}

    stats, client = _run(config, conn, pages, source)

    assert stats.skipped_old == 1
    assert stats.inserted == 0
    assert client.requested == [FEED_URL]  # 本文は取りに行かない


def test_already_collected_articles_are_not_refetched(config, conn, source) -> None:
    """再実行しても同じ記事を取りに行かない（相手サイトへの負荷を増やさない）。"""
    now = datetime.now(timezone.utc)
    pages = {
        FEED_URL: _feed([("Loft", "https://www.dezeen.com/d/", now)]),
        "https://www.dezeen.com/d": _article("Now for sale, asking price $1,000,000."),
    }

    first, _ = _run(config, conn, pages, source)
    assert first.inserted == 1

    second, client = _run(config, conn, pages, source)
    assert second.inserted == 0
    assert second.skipped_known == 1
    assert client.requested == [FEED_URL]
    (count,) = conn.execute("SELECT COUNT(*) FROM properties").fetchone()
    assert count == 1


def test_robots_disallowed_article_is_skipped_not_fetched_anyway(config, conn, source) -> None:
    now = datetime.now(timezone.utc)
    pages = {FEED_URL: _feed([("Blocked", "https://www.dezeen.com/e/", now)])}

    stats, _ = _run(
        config, conn, pages, source, disallowed={"https://www.dezeen.com/e"}
    )

    assert stats.skipped_robots == 1
    assert stats.inserted == 0
    (count,) = conn.execute("SELECT COUNT(*) FROM properties").fetchone()
    assert count == 0


def test_dry_run_does_not_write_to_the_database(config, conn, source) -> None:
    now = datetime.now(timezone.utc)
    pages = {
        FEED_URL: _feed([("Loft", "https://www.dezeen.com/f/", now)]),
        "https://www.dezeen.com/f": _article("For sale. Asking price $900,000."),
    }

    stats, _ = _run(config, conn, pages, source, dry_run=True)

    assert stats.inserted == 1
    (count,) = conn.execute("SELECT COUNT(*) FROM properties").fetchone()
    assert count == 0


def test_falls_back_to_feed_content_when_the_article_page_is_blocked(
    config, conn, source
) -> None:
    """記事ページが 403 でも、フィードが配信している本文で判定を続ける。

    User-Agent を偽装して回避することはしない。
    """
    now = datetime.now(timezone.utc)
    body = (
        "<p>The converted firehouse is now for sale, asking price $2.4 million.</p>"
        "<img src='/hero.jpg'>"
    )
    pages = {FEED_URL: _feed([("Loft", "https://www.dezeen.com/h/", now)], description=body)}

    stats, _ = _run(
        config, conn, pages, source, forbidden={"https://www.dezeen.com/h"}
    )

    assert stats.inserted == 1
    assert stats.used_feed_only == 1
    assert stats.article_fetch_failed == 1
    row = conn.execute("SELECT * FROM properties").fetchone()
    assert "asking price" in row["for_sale_evidence"].lower()
    assert row["content_text"]
    assert row["thumbnail_url"] == "https://www.dezeen.com/hero.jpg"


def test_stops_requesting_article_pages_after_repeated_failures(config, conn, source) -> None:
    """全滅するサイトに無駄なリクエストを送り続けない。"""
    now = datetime.now(timezone.utc)
    entries = [(f"L{i}", f"https://www.dezeen.com/i{i}/", now) for i in range(6)]
    body = "<p>Now for sale. Asking price $1,000,000.</p>"
    pages = {FEED_URL: _feed(entries, description=body)}
    forbidden = {f"https://www.dezeen.com/i{i}" for i in range(6)}

    stats, client = _run(config, conn, pages, source, forbidden=forbidden)

    limit = config.collect.article_fetch_failure_limit
    article_requests = [u for u in client.requested if u != FEED_URL]
    assert len(article_requests) == limit      # 上限に達したら取得をやめる
    assert stats.inserted == 6                 # それでも候補化は続く
    assert stats.used_feed_only == 6


def test_article_fetching_can_be_disabled_entirely(config, conn, source) -> None:
    now = datetime.now(timezone.utc)
    body = "<p>For sale. Asking price $800,000.</p>"
    pages = {FEED_URL: _feed([("Loft", "https://www.dezeen.com/j/", now)], description=body)}

    config = config.model_copy(deep=True)
    config.collect.fetch_article_pages = False
    stats, client = _run(config, conn, pages, source)

    assert client.requested == [FEED_URL]
    assert stats.inserted == 1
    assert stats.used_feed_only == 1


def test_entry_without_any_body_is_not_a_candidate(config, conn, source) -> None:
    """フィードにも記事にも本文が無ければ候補化しない。"""
    now = datetime.now(timezone.utc)
    pages = {FEED_URL: _feed([("", "https://www.dezeen.com/k/", now)])}

    stats, _ = _run(config, conn, pages, source, forbidden={"https://www.dezeen.com/k"})

    assert stats.inserted == 0
    assert stats.fetch_failed == 1


def test_limit_caps_the_number_of_candidates(config, conn, source) -> None:
    now = datetime.now(timezone.utc)
    entries = [(f"L{i}", f"https://www.dezeen.com/g{i}/", now) for i in range(5)]
    pages = {FEED_URL: _feed(entries)}
    for i in range(5):
        pages[f"https://www.dezeen.com/g{i}"] = _article(
            "For sale with an asking price of $1,500,000."
        )

    stats, _ = _run(config, conn, pages, source, limit=2)

    assert stats.inserted == 2


def test_explain_reports_scores_below_the_threshold(config, conn, source) -> None:
    """候補0件のとき、フィード本文が薄いのか販売物件が無いのかを切り分けられること。"""
    now = datetime.now(timezone.utc)
    entries = [
        ("Just a house", "https://www.dezeen.com/m1/", now),
        ("Listed home", "https://www.dezeen.com/m2/", now),
    ]
    pages = {
        FEED_URL: _feed(entries, description="<p>A house completed in 2019.</p>"),
        "https://www.dezeen.com/m1": _article("A house completed in 2019."),
        "https://www.dezeen.com/m2": _article("The home is currently listed for viewing."),
    }

    stats, _ = _run(config, conn, pages, source)
    assert stats.explanations == []          # 既定では収集しない

    stats, _ = _run(config, conn, pages, source, explain=True)

    assert stats.inserted == 0
    assert len(stats.explanations) == 2
    report = stats.explain_report()
    assert "判定内訳" in report
    assert "本文の長さ" in report
    # キーワードだけ拾えた記事が上位に来る
    assert stats.explanations and max(e.score for e in stats.explanations) == 1


def test_listing_only_media_does_not_require_for_sale_signals(
    config, conn, listing_source
) -> None:
    """掲載記事すべてが売出中のメディアでは、シグナル不足でも候補化する。

    CIRCA Old Houses が本文にキーワードを含まないため0件だった実例に対応。
    """
    now = datetime.now(timezone.utc)
    pages = {
        FEED_URL: _feed(
            [("A modernist bungalow", "https://www.wowhaus.co.uk/p1/", now)],
            description="<p>A 1960s bungalow in Kent. Three bedrooms.</p>",
        )
    }

    stats, _ = _run(config, conn, pages, listing_source, forbidden={"https://www.wowhaus.co.uk/p1"})

    assert stats.inserted == 1
    assert stats.no_signal == 0
    row = conn.execute("SELECT * FROM properties").fetchone()
    assert row["source"] == "wowhaus"
    assert "販売中の物件のみを掲載" in row["for_sale_evidence"]
    assert row["signal_score"] == 0        # シグナルは0点だが候補化されている
    assert row["is_for_sale"] is None      # 最終判断はスコアリングに委ねる


def test_editorial_media_still_requires_signals(config, conn, source) -> None:
    """通常の編集ソースでは足切りが効いたままであること。"""
    now = datetime.now(timezone.utc)
    pages = {
        FEED_URL: _feed(
            [("A house", "https://www.dezeen.com/n1/", now)],
            description="<p>A 1960s bungalow in Kent. Three bedrooms.</p>",
        )
    }

    stats, _ = _run(config, conn, pages, source, forbidden={"https://www.dezeen.com/n1"})

    assert stats.inserted == 0
    assert stats.no_signal == 1


def test_url_pattern_filters_out_non_property_pages(config, conn, listing_source) -> None:
    """フィードに混ざるエージェント紹介ページを、取得する前に除外する。

    CIRCA Old Houses で人名のページが5件取り込まれた実例に対応。
    """
    now = datetime.now(timezone.utc)
    listing_source.url_exclude = [r"/(barry|cynthia)-"]
    pages = {
        FEED_URL: _feed(
            [
                ("Barry & Rebecca Richards", "https://www.wowhaus.co.uk/barry-rebecca/", now),
                ("A modernist house", "https://www.wowhaus.co.uk/oldhouse/123/", now),
            ],
            description="<p>A 1960s house.</p>",
        )
    }

    stats, client = _run(config, conn, pages, listing_source,
                         forbidden={"https://www.wowhaus.co.uk/oldhouse/123"})

    assert stats.skipped_url_pattern == 1
    assert stats.inserted == 1
    # 除外したURLには一切アクセスしていない
    assert not any("barry" in url for url in client.requested)


def test_url_include_restricts_to_listing_paths(config, conn, listing_source) -> None:
    now = datetime.now(timezone.utc)
    listing_source.url_include = [r"/oldhouse/"]
    pages = {
        FEED_URL: _feed(
            [
                ("Agent", "https://www.wowhaus.co.uk/some-person/", now),
                ("House", "https://www.wowhaus.co.uk/oldhouse/1/", now),
            ],
            description="<p>A house.</p>",
        )
    }

    stats, _ = _run(config, conn, pages, listing_source,
                    forbidden={"https://www.wowhaus.co.uk/oldhouse/1"})

    assert stats.skipped_url_pattern == 1
    assert stats.inserted == 1
    row = conn.execute("SELECT source_url FROM properties").fetchone()
    assert "/oldhouse/" in row["source_url"]


def test_source_can_opt_out_of_article_fetching(config, conn, listing_source) -> None:
    """Crawl-delay が長いサイトでは、フィード1回のリクエストで済ませる。"""
    now = datetime.now(timezone.utc)
    listing_source.fetch_article_pages = False
    pages = {
        FEED_URL: _feed(
            [("House", "https://www.wowhaus.co.uk/h/", now)],
            description="<p>A 1960s house for sale, asking price £450,000.</p>",
        )
    }

    stats, client = _run(config, conn, pages, listing_source)

    assert client.requested == [FEED_URL]     # 記事ページは取りに行かない
    assert stats.inserted == 1
    assert stats.used_feed_only == 1
