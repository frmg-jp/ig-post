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


# --- 取りこぼしの診断 ---------------------------------------------------
#
# 「1点で落ちた」だけでは、キーワード表現が足りないのか価格の書式が
# 合わないのかが分からない。原因が分からないまま設定をいじると、
# CIRCA の assume_for_sale や listing_link_score のときと同じ失敗になる。


def _explain(text: str):
    from freming.collect.editorial import Explanation

    result = signals.detect(text, [], SIGNALS)
    return Explanation(
        url="https://example.com/a", title="t", score=result.score,
        text_chars=len(text), from_feed_only=False,
        keywords=list(result.keywords), prices=list(result.prices),
        ignored_prices=list(result.ignored_prices),
        keyword_context=result.keyword_context,
        price_context=result.price_context,
    )


def test_price_without_a_keyword_points_at_the_keyword_list() -> None:
    """価格はあるがキーワードが無い＝キーワードの表現漏れ。"""
    explanation = _explain(
        "This neoclassical apartment retains its mouldings. "
        "It is available through Maisons Marine at €1,200,000."
    )
    miss = explanation.near_miss
    assert "販売キーワードが無い" in miss
    assert "keywords" in miss
    # どの表現を足せばよいか分かるよう、価格の周辺を出す
    assert "available through" in miss


def test_keyword_without_a_price_points_at_the_price_patterns() -> None:
    """キーワードはあるが価格が無い＝価格の書式漏れか、本文に価格が無い。

    「price on application」は価格シグナルとして数えるようになったので、
    ここでは価格にまったく触れていない本文を使う。
    """
    explanation = _explain(
        "This finca near Alicante has just come on the market. "
        "The property is being offered directly by the architect."
    )
    miss = explanation.near_miss
    assert "価格を検出できず" in miss
    assert "price_patterns" in miss
    assert "on the market" in miss


def test_distant_price_points_at_the_window_setting() -> None:
    """遠すぎて除外された場合は、距離の設定が原因だと分かるようにする。"""
    filler = "word " * 100
    explanation = _explain(f"This house is for sale. {filler} The stadium cost €240 million.")
    miss = explanation.near_miss
    assert "遠いため除外" in miss
    assert "price_requires_keyword_within" in miss


def test_a_real_candidate_gets_no_near_miss_note() -> None:
    """候補になったものに診断は出さない（読むところを増やさない）。"""
    explanation = _explain(
        "The converted firehouse is now for sale with an asking price of $2.4 million."
    )
    assert explanation.near_miss == ""
    assert "→" not in explanation.line()


def test_articles_with_no_signal_at_all_get_no_note() -> None:
    """そもそも何も無い記事に理由を出しても意味がない。"""
    explanation = _explain("A survey of five exhibitions across Europe this month.")
    assert explanation.near_miss == ""


# --- 実データで見つけた取りこぼし（2026-08-03 thespaces） ----------------


def test_listing_at_is_recognised_as_a_sale_phrase() -> None:
    """「listed for」はあったが「listing at」が無く、実物件を0点にしていた。"""
    text = (
        "Preserving many of the original architectural details before listing at "
        "€1.19 million. Marble fireplaces, decorative mouldings, gilded ceiling medallions."
    )
    result = signals.detect(text, [], SIGNALS)
    assert "listing at" in result.keywords
    assert "€1.19 million" in " ".join(result.prices)
    assert result.is_candidate(SIGNALS.min_signal_score)


def test_price_on_application_counts_as_a_price() -> None:
    """価格非公開の売出物件を取りこぼさない。

    「price upon request」を keywords に置いていたのは分類の誤りだった。
    販売の言い回しではなく価格の状態であり、キーワードは何個あっても
    1点なので、そこに置いても価格非公開の物件は閾値に届かなかった。
    """
    text = (
        "The entire property is on the market with Adelante Homes, "
        "price on application. Photography: courtesy of the architect."
    )
    result = signals.detect(text, [], SIGNALS)
    assert "on the market" in result.keywords
    assert result.prices                       # 価格の状態を価格シグナルとして数える
    assert result.is_candidate(SIGNALS.min_signal_score)


def test_price_on_application_alone_is_not_enough() -> None:
    """物やアートの記事にも「price on application」は出る。単独では候補にしない。"""
    text = "A sculptural writing desk by an Italian maker. Price on application."
    result = signals.detect(text, [], SIGNALS)
    assert result.keywords == []
    assert not result.is_candidate(SIGNALS.min_signal_score)


def test_widened_keywords_do_not_revive_the_stadium_false_positive() -> None:
    """キーワードを足しても、建設費の誤検出は戻らないこと。"""
    filler = "word " * 100
    text = f"This house is for sale. {filler} The nearby stadium cost €240 million."
    result = signals.detect(text, [], SIGNALS)
    assert result.prices == []
    assert not result.is_candidate(SIGNALS.min_signal_score)


# --- 記事要素の内側に混ざるノイズ（2026-08-03 Robb Report） --------------
#
# 見出しが $15.3 Million の記事から $9.3 Million が検出された。関連記事
# ブロックに並んだ別物件の価格を拾っていた。1記事あたり10〜15件の金額が
# 混ざり、複数の記事で同じ本文が出ていた。nav/aside/footer を落とすだけ
# では取れない（ノイズが <article> の内側にある）。


def _page_with_inner_noise(body: str) -> str:
    return f"""
    <html><head><title>Meg Ryan Has Landed a Buyer for Her $15.3 Million Home</title></head>
    <body><article>
      <h1>Meg Ryan Has Landed a Buyer for Her $15.3 Million Home</h1>
      <div class="byline-bio">By Wendy Bowman. Wendy Bowman's Most Recent Stories</div>
      <p>{body} The home was listed for $15.3 million.</p>
      <div class="related-stories">
        <a href="/a">Landis Gores's Home Hits the Market for $3 Million</a>
        <a href="/b">A Napa Estate Asks $9.3 Million</a>
      </div>
      <div class="newsletter-signup">Subscribe for $1 a week</div>
    </article></body></html>
    """


def test_related_stories_inside_the_article_are_stripped() -> None:
    page = parse_page(_page_with_inner_noise("The house sits on two acres. " * 40),
                      "https://robbreport.com/shelter/a")
    assert "Landis Gores" not in page.text
    assert "$9.3 Million" not in page.text


def test_author_and_newsletter_blocks_are_stripped() -> None:
    page = parse_page(_page_with_inner_noise("The house sits on two acres. " * 40),
                      "https://robbreport.com/shelter/a")
    assert "Most Recent Stories" not in page.text
    assert "Subscribe" not in page.text


def test_the_articles_own_price_survives() -> None:
    """ノイズを落としすぎて本文の価格まで消さないこと。"""
    page = parse_page(_page_with_inner_noise("The house sits on two acres. " * 40),
                      "https://robbreport.com/shelter/a")
    result = signals.detect(page.text, page.links, SIGNALS)
    assert "$15.3 million" in page.text
    assert result.prices == ["$15.3 million"]


def test_hits_the_market_is_recognised() -> None:
    """Harvard Five の建築家の住宅が1点で落ちていた実例。"""
    text = (
        "Harvard Five Architect Landis Gores's Connecticut Home Hits the Market "
        "for $3 Million. The 1950s glass house has been restored."
    )
    result = signals.detect(text, [], SIGNALS)
    assert "hits the market" in result.keywords
    assert result.is_candidate(SIGNALS.min_signal_score)


# --- 地の文だけを取り出す（2026-08-03 Robb Report 第2ラウンド） ----------
#
# class 名でノイズを除くやり方は、名前を当てられないと届かない。
# 関連記事と著者の「最近の記事」一覧を落としきれず、山火事の避難記事に
# 別記事の見出し「… Hits the Market for $3 Million」が混ざって売出中と
# 判定された。3つの記事が同じキーワードと同じ価格を持つ形で表面化した。


def _page_with_unlabelled_related(headline: str, body: str) -> str:
    """関連記事一覧に class が付いていないページ（Robb Report と同じ形）。"""
    return f"""
    <html><head><title>{headline}</title></head><body><article>
      <h1>{headline}</h1>
      <div><span>By Wendy Bowman</span>
        <ul><li><a href="/x">Wendy Bowman's Most Recent Stories</a></li></ul></div>
      <p>{body}</p>
      <div><h2>More From Robb Report</h2><ul>
        <li><a href="/a">Landis Gores's Home Hits the Market for $3 Million</a></li>
        <li><a href="/b">A Napa Estate Asks $9.3 Million</a></li>
      </ul></div>
    </article></body></html>
    """


def test_headlines_of_other_articles_do_not_leak_in() -> None:
    page = parse_page(
        _page_with_unlabelled_related(
            "Clooney Forced to Evacuate French Estate",
            "Wildfires forced the couple to leave the estate. " * 30,
        ),
        "https://robbreport.com/shelter/clooney",
    )
    assert "Landis Gores" not in page.text
    assert "$9.3 Million" not in page.text


def test_an_unrelated_article_is_not_made_a_candidate_by_leakage() -> None:
    """山火事の避難記事が「売出中」になっていた実例。"""
    page = parse_page(
        _page_with_unlabelled_related(
            "Clooney Forced to Evacuate French Estate",
            "Wildfires forced the couple to leave the estate. " * 30,
        ),
        "https://robbreport.com/shelter/clooney",
    )
    result = signals.detect(page.text, page.links, SIGNALS)
    assert result.score == 0
    assert not result.is_candidate(SIGNALS.min_signal_score)


def test_the_real_listing_still_scores() -> None:
    """ノイズを落としすぎて本物まで消さないこと。"""
    page = parse_page(
        _page_with_unlabelled_related(
            "Landis Gores's Home Hits the Market for $3 Million",
            "The 1950s glass house has been restored. " * 30
            + " It hits the market for $3 million.",
        ),
        "https://robbreport.com/shelter/landis",
    )
    result = signals.detect(page.text, page.links, SIGNALS)
    assert result.is_candidate(SIGNALS.min_signal_score)


def test_pages_without_paragraphs_keep_their_body() -> None:
    """<p> を使わずに本文を組むページで、本文を丸ごと捨てないこと。"""
    body = "This converted firehouse is for sale at $2.4 million. " * 20
    html = f"<html><head><title>t</title></head><body><article><div>{body}</div></article></body></html>"
    page = parse_page(html, "https://example.com/a")
    assert "converted firehouse" in page.text
    assert signals.detect(page.text, page.links, SIGNALS).is_candidate(
        SIGNALS.min_signal_score
    )
