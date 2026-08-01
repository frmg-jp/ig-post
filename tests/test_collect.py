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


def test_listing_link_alone_reaches_threshold() -> None:
    """不動産サイトへの外部リンクは単独で閾値に届く（重み2）。"""
    result = signals.detect(
        "A striking loft in SOMA.",
        ["https://www.sothebysrealty.com/id/abc123"],
        SIGNALS,
    )
    assert result.listing_links
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
