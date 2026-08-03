"""収集まわり（販売シグナル検出・HTML解析・重複防止）の検証。"""

from __future__ import annotations

import pytest

from freming.collect import signals
from freming.collect.base import Candidate, normalize_url, parse_page
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import exists_source_url, insert_candidate

CONFIG = load_config("config.yaml")
SIGNALS = CONFIG.for_sale_signals


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    return connect(path)


# --- 販売シグナル -----------------------------------------------------


def test_plain_architecture_article_is_not_a_candidate() -> None:
    """単なる建築紹介は候補にしない（編集ソースの大半はこれ）。"""
    text = "The house was completed in 2019 by a Lisbon-based studio. Photography by X."
    result = signals.detect(text, ["https://www.dezeen.com/tag/houses/"], SIGNALS)
    assert result.score == 0
    assert not result.is_candidate(SIGNALS.min_signal_score)


def test_keyword_alone_is_below_threshold() -> None:
    """キーワードだけでは足りない（min_signal_score = 2）。"""
    result = signals.detect("The building is currently listed for viewing.", [], SIGNALS)
    assert result.score == SIGNALS.keyword_score
    assert not result.is_candidate(SIGNALS.min_signal_score)


def test_keyword_and_price_reaches_threshold() -> None:
    text = "This converted firehouse is now for sale with an asking price of $2.4 million."
    result = signals.detect(text, [], SIGNALS)
    assert result.keywords
    assert result.prices
    assert result.is_candidate(SIGNALS.min_signal_score)


def test_listing_link_alone_does_not_reach_threshold() -> None:
    """不動産サイトへのリンクだけでは候補にしない。

    リンクは「売出中」の裏付けとして弱い。CIRCA のエージェント紹介ページが
    本人の Compass プロフィールへリンクしているだけで候補化された実例が
    あったため、単独では閾値に届かないようにしている。
    """
    result = signals.detect(
        "A striking loft in SOMA.",
        ["https://www.sothebysrealty.com/id/abc123"],
        SIGNALS,
    )
    assert result.listing_links
    assert not result.is_candidate(SIGNALS.min_signal_score)


def test_listing_link_with_keyword_reaches_threshold() -> None:
    """リンクに販売キーワードが伴えば候補になる。"""
    result = signals.detect(
        "A striking loft in SOMA is now for sale.",
        ["https://www.sothebysrealty.com/id/abc123"],
        SIGNALS,
    )
    assert result.is_candidate(SIGNALS.min_signal_score)


def test_subdomain_of_listing_site_is_detected() -> None:
    result = signals.detect("x", ["https://listings.compass.com/abc"], SIGNALS)
    assert result.listing_links


def test_unrelated_domain_is_not_a_listing_link() -> None:
    """名前が似ているだけの別ドメインを誤検出しない。"""
    result = signals.detect("x", ["https://notzillow.com/abc"], SIGNALS)
    assert not result.listing_links


def test_evidence_explains_why_it_was_picked() -> None:
    text = "The penthouse is on the market, asking price $3,200,000 through the agency."
    result = signals.detect(text, ["https://www.redfin.com/x"], SIGNALS)
    assert "記載:" in result.evidence
    assert "価格表記:" in result.evidence
    assert "redfin.com" in result.evidence


# --- HTML解析 ---------------------------------------------------------


HTML = """
<html><head>
  <title>Fallback Title</title>
  <meta property="og:title" content="A Firehouse Turned Home">
  <meta property="og:image" content="/img/hero.jpg">
</head><body>
  <script>var noise = 1;</script>
  <p>Now for sale: the 1920s firehouse.</p>
  <a href="/tag/loft">loft</a>
  <a href="https://www.sothebysrealty.com/id/1">listing</a>
  <a href="mailto:x@example.com">mail</a>
</body></html>
"""


def test_parse_page_prefers_og_title_and_absolutizes_urls() -> None:
    page = parse_page(HTML, base_url="https://www.dezeen.com/2026/01/01/firehouse/")
    assert page.title == "A Firehouse Turned Home"
    assert page.thumbnail_url == "https://www.dezeen.com/img/hero.jpg"
    assert "https://www.dezeen.com/tag/loft" in page.links
    assert "https://www.sothebysrealty.com/id/1" in page.links


def test_parse_page_drops_scripts_and_mailto() -> None:
    page = parse_page(HTML, base_url="https://www.dezeen.com/a/")
    assert "var noise" not in page.text
    assert "Now for sale" in page.text
    assert not any(link.startswith("mailto:") for link in page.links)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/a#gallery", "https://example.com/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
        ("https://example.com/", "https://example.com/"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


# --- 重複防止 ---------------------------------------------------------


def test_same_url_is_not_inserted_twice(db) -> None:
    """再実行しても重複納品しないための土台。"""
    candidate = Candidate(source="dezeen", source_url="https://x/1", title="A")
    assert insert_candidate(db, candidate) is not None
    assert insert_candidate(db, candidate) is None
    assert exists_source_url(db, "https://x/1")
    (count,) = db.execute("SELECT COUNT(*) FROM properties").fetchone()
    assert count == 1


def test_inserted_candidate_starts_as_pending_and_unscored(db) -> None:
    candidate = Candidate(
        source="dezeen",
        source_rank="S",
        source_url="https://x/2",
        for_sale_evidence="asking price $1M",
        signal_score=2,
    )
    property_id = insert_candidate(db, candidate)
    row = db.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["score"] is None
    assert row["is_for_sale"] is None      # 販売可否の判断はスコアリングに委ねる
    assert row["signal_score"] == 2
    assert row["source_rank"] == "S"


# --- 価格の文脈判定 ---------------------------------------------------


def test_construction_budget_is_not_counted_as_an_asking_price() -> None:
    """建設費・事業費の金額を売出価格と取り違えないこと。

    Dezeen のスタジアム記事で €240 million などが誤検出された実例に対応。
    """
    text = (
        "The €240 million stadium was completed in 2026. "
        "The Glasgow School of Art rebuild is estimated at £62 million."
    )
    result = signals.detect(text, [], SIGNALS)
    assert result.prices == []
    assert result.ignored_prices          # 金額自体は検出しているが加点しない
    assert result.score == 0
    assert not result.is_candidate(SIGNALS.min_signal_score)


def test_price_next_to_a_for_sale_keyword_is_counted() -> None:
    text = "The converted firehouse is now for sale with an asking price of $2.4 million."
    result = signals.detect(text, [], SIGNALS)
    assert result.prices
    assert result.score >= SIGNALS.min_signal_score


def test_price_far_from_the_keyword_is_ignored() -> None:
    """同じ記事内でも、キーワードから遠い金額は売出価格とみなさない。"""
    filler = "word " * 100          # 約500字
    text = f"This house is for sale. {filler} The nearby stadium cost €240 million."
    result = signals.detect(text, [], SIGNALS)
    assert "€240 million" in " ".join(result.ignored_prices)
    assert result.prices == []
    assert result.score == SIGNALS.keyword_score      # キーワード分のみ


# --- 手動入力（自動収集が禁止されているサイト用） -----------------------


def test_manual_entry_does_not_fetch_the_page(db, monkeypatch) -> None:
    """Zillow等は一切アクセスせず、人が入力した内容だけで候補化する。"""
    from freming.collect import manual
    from freming.net.client import HttpClient

    def _boom(*_args, **_kwargs):  # pragma: no cover - 呼ばれたら失敗
        raise AssertionError("手動入力でHTTPアクセスしてはいけない")

    monkeypatch.setattr(HttpClient, "__init__", _boom)

    property_id = manual.add_manual_entry(
        CONFIG,
        source_url="https://www.zillow.com/homedetails/123_Example_St/999_zpid/",
        title="1920s firehouse conversion",
        price="$2,400,000",
        city="San Francisco",
        country="United States",
        conn=db,
    )

    row = db.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    assert row["source"] == "zillow"          # ドメインからソースを推定
    assert row["source_rank"] == "B"
    assert row["price"] == "$2,400,000"
    assert row["location_city"] == "San Francisco"
    assert row["is_for_sale"] == 1            # 人が確認済み
    assert "手動入力" in row["for_sale_evidence"]
    assert row["status"] == "pending"
    assert row["score"] is None               # スコアリングは通常どおり行う


def test_manual_entry_is_not_duplicated(db) -> None:
    from freming.collect import manual

    url = "https://www.redfin.com/CA/San-Francisco/1-Main-St/home/12345"
    manual.add_manual_entry(CONFIG, source_url=url, title="Loft", conn=db)
    with pytest.raises(manual.AlreadyCollected):
        manual.add_manual_entry(CONFIG, source_url=url, title="Loft（重複）", conn=db)


# ----------------------------------------------------------------------
# ページの共通部分を本文に混ぜない
#
# 2026-08-03、thespaces.com で全記事が例外なく販売シグナル1点になった。
# ナビゲーションに "Property"、全ページ共通の掲載枠に "for sale" と
# "listed for" が入っており、美術展のまとめ記事にまで点が付いていた。
# 全記事が同じ点数になると、シグナルは何の情報も持たなくなる。
# ----------------------------------------------------------------------
def _page_with_chrome(body_text: str) -> str:
    return f"""
    <html><head><title>August art round-up - The Spaces</title></head>
    <body>
      <nav>The Spaces Property Architecture &amp; Design About us</nav>
      <aside><h3>Property for sale</h3>
        <p>This apartment is listed for €1,200,000</p>
        <a href="https://www.zillow.com/homedetails/1_zpid/">See listing</a>
      </aside>
      <article><header><h1>August art round-up</h1></header><p>{body_text}</p></article>
      <footer>Homes for sale in London.</footer>
    </body></html>
    """


def test_navigation_is_not_part_of_the_article() -> None:
    html = _page_with_chrome("A survey of five exhibitions. " * 30)
    page = parse_page(html, "https://thespaces.com/a")
    assert "About us" not in page.text
    assert "Architecture & Design About" not in page.text


def test_sidebar_listings_do_not_leak_into_the_article() -> None:
    """共通の掲載枠が本文に入ると、記事の内容と無関係に販売シグナルが付く。"""
    html = _page_with_chrome("A survey of five exhibitions. " * 30)
    page = parse_page(html, "https://thespaces.com/a")
    assert "listed for" not in page.text
    assert "€1,200,000" not in page.text


def test_footer_is_not_part_of_the_article() -> None:
    html = _page_with_chrome("A survey of five exhibitions. " * 30)
    page = parse_page(html, "https://thespaces.com/a")
    assert "Homes for sale in London" not in page.text


def test_links_come_from_the_article_only() -> None:
    """サイドバーの仲介サイトへのリンクを「売出中の裏付け」にしない。"""
    html = _page_with_chrome("A survey of five exhibitions. " * 30)
    page = parse_page(html, "https://thespaces.com/a")
    assert not any("zillow" in url for url in page.links)


def test_headline_keywords_survive_the_stripping() -> None:
    """見出しの「for Sale」を落とすと、本物の売出記事を取りこぼす。"""
    body = "This mid-century home has been carefully updated. " * 30
    html = f"""
    <html><head><title>Updated Bridlemile Home for Sale in SW Portland</title></head>
    <body><nav>Home About</nav>
      <article><header><h1>Updated Bridlemile Home for Sale</h1></header>
      <p>{body} The asking price is $899,000.</p></article>
    </body></html>
    """
    page = parse_page(html, "https://example.com/a")
    assert "for Sale" in page.text
    assert "$899,000" in page.text


def test_short_article_element_falls_back_to_the_whole_page() -> None:
    """本文要素が短すぎるときに掴むと、本文をほとんど捨ててしまう。"""
    body = "The full article lives outside the article element. " * 30
    html = f"""
    <html><head><title>t</title></head><body>
      <article><p>Photo credit</p></article>
      <div class="story"><p>{body}</p></div>
    </body></html>
    """
    page = parse_page(html, "https://example.com/a")
    assert "The full article lives outside" in page.text


def test_feed_fragments_still_work() -> None:
    """フィード配信のHTMLは断片で、article も nav も無い。"""
    html = "<div><p>A converted warehouse is for sale at $2,400,000.</p></div>"
    page = parse_page(html, "https://example.com/a")
    assert "converted warehouse" in page.text
    assert "$2,400,000" in page.text
