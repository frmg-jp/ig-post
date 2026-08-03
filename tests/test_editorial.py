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
    """「編集メディア掲載 かつ 販売中」を拾えること。これが Hidden Gem の正体。

    リンク単独では足りないので、販売キーワードと合わせて閾値に届く。
    """
    now = datetime.now(timezone.utc)
    pages = {
        FEED_URL: _feed([("Loft", "https://www.dezeen.com/c/", now)]),
        "https://www.dezeen.com/c": _article(
            "A rare SOMA loft is now for sale.",
            links='<a href="https://www.sothebysrealty.com/id/999">listing</a>',
        ),
    }

    stats, _ = _run(config, conn, pages, source)

    assert stats.inserted == 1
    row = conn.execute("SELECT * FROM properties").fetchone()
    assert "sothebysrealty.com" in row["for_sale_evidence"]
    assert row["signal_score"] >= config.for_sale_signals.min_signal_score


def test_agent_profile_linking_to_a_brokerage_is_not_a_candidate(config, conn, source) -> None:
    """エージェント紹介ページを物件として取り込まないこと。

    CIRCA のフィードで実際に起きた誤検出。本人の Compass プロフィールへの
    リンクがあるだけで候補化されていた。
    """
    now = datetime.now(timezone.utc)
    pages = {
        FEED_URL: _feed([("Ann Gluck", "https://www.dezeen.com/ann-gluck/", now)]),
        "https://www.dezeen.com/ann-gluck": _article(
            "ANN GLUCK Compass Pasadena, CA email: ann.gluck (at) compass (dot) com",
            links='<a href="https://www.compass.com/agents/ann-gluck/">profile</a>',
        ),
    }

    stats, _ = _run(config, conn, pages, source)

    assert stats.inserted == 0
    assert stats.no_signal == 1


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


def test_probe_shows_every_entry_regardless_of_state(config, conn, source) -> None:
    """調査用の probe は、期間外・取得済みでも内訳を表示すること。"""
    old = datetime.now(timezone.utc) - timedelta(days=config.collect.lookback_days + 100)
    pages = {
        FEED_URL: _feed([("Old entry", "https://www.dezeen.com/z1/", old)],
                        description="<p>A house.</p>"),
    }

    # 通常の収集では期間外として弾かれる
    normal, _ = _run(config, conn, pages, source)
    assert normal.skipped_old == 1
    assert normal.explanations == []

    # 調査用の条件（期間無制限・取得済み無視）では内訳が出る
    probe_config = config.model_copy(deep=True)
    probe_config.collect.lookback_days = 36500
    probe_config.collect.fetch_article_pages = False
    client = FakeClient(pages)
    stats = EditorialCollector(probe_config, client, conn).collect(
        source, dry_run=True, explain=True, ignore_known=True
    )
    assert len(stats.explanations) == 1
    assert "https://www.dezeen.com/z1" in stats.explanations[0].url
    assert "https://www.dezeen.com/z1" in stats.explain_report()


def test_feed_failure_reason_is_specific(config, conn, source) -> None:
    """読めなかった理由が「記事0件」で潰れず、原因ごとに残ること。

    URLの誤り・robots による拒否・フィード形式でない、は対処が別なので
    出力で区別できる必要がある。
    """
    # robots.txt による拒否
    stats, _ = _run(config, conn, {}, source, disallowed={FEED_URL})
    assert stats.skipped_robots == 1
    assert "robots" in stats.feed_failures[0]

    # フィードではないHTMLが返ってきた場合
    stats, _ = _run(config, conn, {FEED_URL: "<html><body>not a feed</body></html>"}, source)
    assert stats.feed_entries == 0
    assert "フィード形式ではない" in stats.feed_failures[0]

    # 取得そのものに失敗した場合
    stats, _ = _run(config, conn, {}, source, forbidden={FEED_URL})
    assert stats.fetch_failed == 1
    assert stats.feed_failures


def test_failure_reason_explains_http_status() -> None:
    """HTTPステータスは、次に取るべき行動が分かる文言にすること。"""
    import httpx

    from freming.collect.editorial import failure_reason

    request = httpx.Request("GET", "https://example.com/feed/")
    for status, expected in ((404, "404"), (403, "403"), (429, "429")):
        exc = httpx.HTTPStatusError(
            "err", request=request, response=httpx.Response(status, request=request)
        )
        assert expected in failure_reason(exc)

    assert "接続できない" in failure_reason(httpx.ConnectError("no host"))


def test_discover_feeds_reads_declared_links(config, monkeypatch) -> None:
    """トップページの <link rel=alternate> からフィードURLを拾うこと。

    /feed/ のような推測に頼らないための機能なので、宣言されたURLを
    そのまま（相対パスは絶対化して）返せることを確認する。
    """
    from freming.collect import editorial

    html = (
        "<html><head>"
        "<link rel='alternate' type='application/rss+xml' title='Posts' href='/rss/posts'>"
        "<link rel='alternate' type='application/atom+xml' href='https://cdn.example.com/atom'>"
        "<link rel='alternate' type='text/html' href='/mobile'>"        # フィードではない
        "<link rel='stylesheet' href='/style.css'>"
        "</head><body></body></html>"
    )

    class _Resp:
        text = html
        url = "https://example.com/"

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **_kwargs):
            return _Resp()

    monkeypatch.setattr(editorial, "HttpClient", lambda *_a, **_k: _Client())

    feeds = editorial.discover_feeds(config, "https://example.com/")
    assert [url for url, _ in feeds] == [
        "https://example.com/rss/posts",
        "https://cdn.example.com/atom",
    ]


def test_url_pattern_report_groups_by_path(config, conn, source) -> None:
    """フィードに混ざるURLの種類が1回の取得で見えること。

    CIRCA のように物件ページとエージェント紹介ページが同居するソースで、
    url_exclude に何を書けばよいかを判断するための出力。
    """
    now = datetime.now(timezone.utc)
    entries = [
        ("House A", "https://www.dezeen.com/property/house-a/", now),
        ("House B", "https://www.dezeen.com/property/house-b/", now),
        ("Agent C", "https://www.dezeen.com/agent/carol/", now),
    ]
    pages = {FEED_URL: _feed(entries, description="<p>A house.</p>")}

    probe_config = config.model_copy(deep=True)
    probe_config.collect.fetch_article_pages = False
    stats = EditorialCollector(probe_config, FakeClient(pages), conn).collect(
        source, dry_run=True, explain=True, ignore_known=True
    )

    report = stats.url_pattern_report()
    assert "/property/" in report
    assert "/agent/" in report
    assert "2件" in report and "1件" in report


def test_excluded_urls_are_still_reported(config, conn, source) -> None:
    """除外されたURLも記録すること。効きすぎているかを確認するため。"""
    now = datetime.now(timezone.utc)
    src = source.model_copy(deep=True)
    src.url_exclude = [r"/agent/"]
    pages = {
        FEED_URL: _feed(
            [("Agent C", "https://www.dezeen.com/agent/carol/", now)],
            description="<p>A house.</p>",
        )
    }

    stats = EditorialCollector(config, FakeClient(pages), conn).collect(src, dry_run=True)

    assert stats.skipped_url_pattern == 1
    assert "/agent/" in stats.url_pattern_report()


# ----------------------------------------------------------------------
# 配信ペース（フィードの窓が何日分か）
# ----------------------------------------------------------------------
def _stats_with_dates(days_ago: list[float], entries: int, inserted: int):
    """公開日を指定した CollectStats を組み立てる。"""
    from datetime import datetime, timedelta, timezone

    from freming.collect.editorial import CollectStats

    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    stats = CollectStats(source="test")
    stats.feed_entries = entries
    stats.inserted = inserted
    stats.entry_dates = [now - timedelta(days=d) for d in days_ago]
    return stats


def test_window_is_measured_in_days() -> None:
    """10件が何日分かが分からないと、1日あたりの本数が出せない。"""
    stats = _stats_with_dates([0, 2, 4, 6], entries=4, inserted=2)
    assert stats.window_days == pytest.approx(6.0)
    # 窓の両端しか分からないので、区間の数（件数-1）で割る
    assert stats.entries_per_day == pytest.approx(3 / 6)


def test_weekly_candidates_combine_pace_and_ratio() -> None:
    """審査に上がる件数 = 配信ペース × 候補率。"""
    stats = _stats_with_dates([0, 2, 4, 6], entries=4, inserted=2)
    # 0.5本/日 × 候補率0.5 × 7日 = 1.75件/週
    assert stats.candidates_per_week == pytest.approx(1.75)


def test_pace_is_unknown_without_dates() -> None:
    """公開日を配信しないフィードもある。分からないものは分からないと出す。"""
    stats = _stats_with_dates([], entries=10, inserted=4)
    assert stats.window_days is None
    assert stats.entries_per_day is None
    assert stats.candidates_per_week is None
    assert "測れません" in stats.pace_report()


def test_pace_is_unknown_when_everything_shares_one_timestamp() -> None:
    """全件が同時刻だと 0 で割ることになる。0件/日と言い切らない。"""
    stats = _stats_with_dates([1, 1, 1], entries=3, inserted=1)
    assert stats.window_days is None
    assert "同一" in stats.pace_report()


def test_pace_report_says_how_many_entries_lacked_a_date() -> None:
    """一部だけ日付が無い場合、何件を計算から外したかを出す。"""
    stats = _stats_with_dates([0, 3], entries=10, inserted=4)
    report = stats.pace_report()
    assert "8件は公開日が取れず" in report


def test_pace_report_shows_the_window_and_the_weekly_estimate() -> None:
    stats = _stats_with_dates([0, 7], entries=2, inserted=1)
    report = stats.pace_report()
    assert "2026-07-27" in report and "2026-08-03" in report
    assert "7.0日分" in report
    assert "週" in report


def test_excerpt_feed_is_not_reported_as_zero_candidates() -> None:
    """抜粋配信では候補が構造的に0になる。0件と言い切ってはいけない。

    thespaces は本文中央値240字で probe の候補0件だったが、記事ページを
    取ると 4/10 が候補になった。probe の数字だけを見て切ると、いま一番
    歩留まりの良いソースを捨てることになる。
    """
    from freming.collect.editorial import Explanation

    stats = _stats_with_dates([0, 4], entries=10, inserted=0)
    stats.explanations = [
        Explanation(url=f"https://ex.com/{i}", title="t", score=0,
                    text_chars=240, from_feed_only=True)
        for i in range(10)
    ]
    assert stats.excerpt_only is True
    assert stats.weekly_is_reliable is False
    assert "抜粋しか配信していません" in stats.pace_report()


def test_full_text_feed_with_no_candidates_is_believed() -> None:
    """本文が厚くて候補0件なら、それは本当に候補が無い。"""
    from freming.collect.editorial import Explanation

    stats = _stats_with_dates([0, 4], entries=10, inserted=0)
    stats.explanations = [
        Explanation(url=f"https://ex.com/{i}", title="t", score=0,
                    text_chars=4500, from_feed_only=True)
        for i in range(10)
    ]
    assert stats.excerpt_only is False
    assert stats.weekly_is_reliable is True
    assert "抜粋" not in stats.pace_report()


def test_disabled_source_can_still_be_dry_run(tmp_path, monkeypatch) -> None:
    """未検証のソースを enabled: false のまま試せること。

    dry-run は書き込まないので止める理由がない。ここで拒むと
    「登録して確かめてから有効化する」手順が踏めない。
    """
    from freming.collect.editorial import CollectStats, collect_source
    from freming.config import load_config

    cfg = load_config("config.yaml").model_copy(deep=True)
    disabled = next(s for s in cfg.editorial_sources if not s.enabled and s.feeds)

    calls: list[str] = []
    monkeypatch.setattr(
        "freming.collect.editorial.EditorialCollector.collect",
        lambda self, source, *a, **k: calls.append(source.key) or CollectStats(source.key),
    )
    monkeypatch.setattr("freming.collect.editorial.connect", lambda _t: _NullConn())

    collect_source(cfg, disabled.key, dry_run=True)
    assert calls == [disabled.key]

    with pytest.raises(SystemExit, match="enabled: false"):
        collect_source(cfg, disabled.key, dry_run=False)


class _NullConn:
    def close(self) -> None:
        pass


# ----------------------------------------------------------------------
# 止まったフィード
#
# 2026-08-03、SocketSite の最新記事が2024年7月だった。窓の長さだけを見ると
# 「0.4本/日」という健全な数字が出るが、実際には2年前に止まっていて
# collect では「期間外 15」で1件も入らない。窓の長さと、その窓がいつの
# ものかは別の情報で、後者を出さないと止まったフィードを採用してしまう。
# ----------------------------------------------------------------------
def _stats_ending(days_ago: float, span: float, count: int = 15, lookback: int = 30):
    from datetime import datetime, timedelta, timezone

    from freming.collect.editorial import CollectStats

    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    stats = CollectStats(source="t", lookback_days=lookback)
    stats.feed_entries = count
    stats.inserted = 1
    stats.entry_dates = [
        now - timedelta(days=days_ago + span * i / (count - 1)) for i in range(count)
    ]
    return stats


def test_a_frozen_feed_is_reported_as_stopped() -> None:
    stats = _stats_ending(days_ago=750, span=32)
    assert stats.is_stale() is True
    assert stats.days_since_newest() == pytest.approx(750, abs=1)
    assert "止まっています" in stats.pace_report()
    # 日数は端数の丸めで 750/751 のどちらにも転ぶ（日付をまたぐと変わる）。
    # 桁を焼き込むと、コードを何も変えていないのに落ちる日が来る。
    assert f"最新記事は {round(stats.days_since_newest())} 日前" in stats.pace_report()


def test_a_frozen_feed_reports_zero_not_its_old_pace() -> None:
    """過去のペースを「見込み」として出さない。"""
    stats = _stats_ending(days_ago=750, span=32)
    assert stats.entries_per_day == pytest.approx(14 / 32)   # 過去のペースは残す
    assert stats.candidates_per_week == 0.0                  # 見込みは0


def test_a_live_feed_is_not_flagged() -> None:
    stats = _stats_ending(days_ago=2, span=8)
    assert stats.is_stale() is False
    assert "止まっています" not in stats.pace_report()
    assert stats.candidates_per_week is not None
    assert stats.candidates_per_week > 0


def test_staleness_is_measured_against_the_collection_window() -> None:
    """基準は collect.lookback_days。probe が期間を外しても本来の値で判定する。"""
    stats = _stats_ending(days_ago=45, span=10, lookback=30)
    assert stats.is_stale() is True
    stats.lookback_days = 90
    assert stats.is_stale() is False


def test_probe_uses_the_real_lookback_for_staleness(monkeypatch) -> None:
    """probe は期間フィルタを 36500 日に緩めるが、停止判定はそれを使わない。

    緩めた値で判定すると、2年前に止まったフィードも「動いている」と出る。
    """
    from freming.collect.editorial import EditorialCollector
    from freming.config import load_config

    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.collect.lookback_days = 36500
    collector = EditorialCollector(cfg, client=None, conn=None)
    collector.real_lookback_days = 30

    source = next(s for s in cfg.editorial_sources if s.feeds)
    source = source.model_copy(update={"feeds": []})   # フィード取得はしない
    stats = collector.collect(source, dry_run=True)
    assert stats.lookback_days == 30


def test_url_excluded_entries_still_count_towards_the_pace(monkeypatch) -> None:
    """url_exclude で落とした記事も配信ペースには数える。

    配信ペースはフィードの窓全体で決まる。除外してから数えると窓が縮んで
    見え、ペースが実際より遅く出る。実例（Robb Report）: 除外した3件の分だけ
    窓が縮み 2.8本/日 が 1.9本/日 になり、さらに「3件は公開日が取れず」と
    いう誤った注記まで出ていた（公開日はあり、除外しただけ）。
    """
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from freming.collect.editorial import CollectStats, EditorialCollector
    from freming.config import EditorialSource, load_config

    cfg = load_config("config.yaml").model_copy(deep=True)
    collector = EditorialCollector(cfg, client=None, conn=None)
    source = EditorialSource(
        key="t", name="t", rank="B", enabled=True,
        url_exclude=["/art-collectibles/"],
    )
    stats = CollectStats(source="t", lookback_days=30)
    cutoff = datetime(2026, 8, 3, tzinfo=timezone.utc) - timedelta(days=30)

    def _entry(url: str, days_ago: float):
        published = datetime(2026, 8, 3, tzinfo=timezone.utc) - timedelta(days=days_ago)
        return SimpleNamespace(
            link=url, title="t", published_parsed=published.timetuple()[:6]
        )

    # すべて url_exclude に当たる記事。除外されても窓には数える。
    for days in (0.0, 1.5, 3.0):
        collector._process_entry(
            _entry(f"https://ex.com/shelter/art-collectibles/{days}", days),
            source, cutoff, stats, dry_run=True,
        )

    assert stats.skipped_url_pattern == 3
    assert len(stats.entry_dates) == 3          # 除外しても日付は残る
    assert stats.window_days == pytest.approx(3.0)
    assert "公開日が取れず" not in stats.pace_report()
