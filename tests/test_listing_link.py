"""記事の中にある販売ページURLの取り出しの検証。

ストーリーズに貼るのは「その家が買えるページ」。記事の末尾に
Compass / Zillow へのリンクがあるので、そこから拾う。**販売サイトへは
こちらから接続しない**ので、抽出は記事のHTMLだけで完結する。
"""

from __future__ import annotations

import pytest

from freming.collect import signals
from freming.collect.base import parse_page
from freming.collect.relink import pending_rows, relink
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate

CONFIG = load_config("config.yaml")


# --- どのリンクを選ぶか -----------------------------------------------
def test_物件ページを選ぶ() -> None:
    assert signals.pick_listing_url(
        ["https://www.compass.com/listing/521-ne-6th-street-gainesville-fl-32601/1234567890/"]
    ) == "https://www.compass.com/listing/521-ne-6th-street-gainesville-fl-32601/1234567890/"


def test_業者紹介は選ばない() -> None:
    """CIRCA でエージェント紹介ページを拾った（2026-08-01）。貼っても家が出ない。"""
    assert signals.pick_listing_url(["https://www.compass.com/agents/jane-doe/"]) is None


def test_トップページは選ばない() -> None:
    assert signals.pick_listing_url(["https://www.zillow.com/"]) is None


def test_物件ページが業者紹介より優先される() -> None:
    found = signals.pick_listing_url([
        "https://www.compass.com/agents/jane-doe/",
        "https://www.compass.com/listing/1-elm-st/999/",
    ])
    assert found == "https://www.compass.com/listing/1-elm-st/999/"


def test_番地や掲載IDのあるものを優先する() -> None:
    found = signals.pick_listing_url([
        "https://www.zillow.com/homes/for_sale/",
        "https://www.zillow.com/homedetails/521-NE-6th-St-Gainesville-FL-32601/43210_zpid/",
    ])
    assert "43210_zpid" in found


def test_候補が無ければNone() -> None:
    assert signals.pick_listing_url([]) is None


# --- 記事HTMLから通しで ------------------------------------------------
ARTICLE = """
<html><head><title>A House in Gainesville</title></head><body>
<article>
  <p>Location: 521 Northeast 6th Street, Gainesville, Florida.
     The 1,962-square-foot home is on the market for $649,000 and is
     listed for sale by the owner. It has been restored over three years
     with original heart pine floors, a new roof and a rebuilt porch.
     The garden was replanted with native species. The house sits on a
     quarter-acre lot in the Duckpond district, a block from the park.</p>
  <p>See the full listing at
     <a href="https://www.compass.com/listing/521-ne-6th-st/12345/">Compass</a>.</p>
</article>
</body></html>
"""


def _detect(html: str):
    page = parse_page(html, base_url="https://www.6sqft.com/a-house/")
    return signals.detect(page.text, page.links, CONFIG.for_sale_signals)


def test_記事末尾のリンクを拾う() -> None:
    found = signals.pick_listing_url(_detect(ARTICLE).listing_links)
    assert found == "https://www.compass.com/listing/521-ne-6th-st/12345/"


# --- 既存分への後追い --------------------------------------------------
class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeClient:
    """記事だけを返す。**販売サイトを叩いたら落とす。**"""

    def __init__(self, html: str = ARTICLE) -> None:
        self.html = html
        self.urls: list[str] = []

    def get(self, url: str):
        if any(host in url for host in ("zillow.com", "compass.com", "redfin.com")):
            raise AssertionError(f"販売サイトへ接続しています: {url}")
        self.urls.append(url)
        return FakeResponse(self.html)

    def close(self) -> None:
        pass


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    return connect(path)


def _add(conn, *, source="dwell", status="delivered", url=None) -> int:
    cursor = conn.execute(
        "INSERT INTO properties (source, source_url, title, status, score, collected_at) "
        "VALUES (?, ?, 'A House', ?, 70, '2026-08-01T00:00:00+00:00') RETURNING id",
        (source, url or f"https://www.6sqft.com/{status}{source}/", status),
    )
    property_id = cursor.fetchone()["id"]
    conn.commit()
    return property_id


def test_記事を読み直して販売ページを入れる(db) -> None:
    property_id = _add(db)
    client = FakeClient()
    stats = relink(CONFIG, db, client=client)

    assert stats.filled == 1
    row = db.execute("SELECT listing_url FROM properties WHERE id = ?",
                     (property_id,)).fetchone()
    assert row["listing_url"] == "https://www.compass.com/listing/521-ne-6th-st/12345/"
    # 読みに行ったのは記事だけ
    assert client.urls == ["https://www.6sqft.com/delivereddwell/"]


def test_既に入っている行は読み直さない(db) -> None:
    property_id = _add(db)
    db.execute("UPDATE properties SET listing_url = 'https://example.com/x' WHERE id = ?",
               (property_id,))
    db.commit()
    assert pending_rows(db) == []


def test_記事にリンクが無ければ入れない(db) -> None:
    _add(db)
    stats = relink(CONFIG, db, client=FakeClient("<html><body><p>ただの記事。</p></body></html>"))
    assert stats.filled == 0
    assert stats.not_found == 1


def test_納品済みが先に来る(db) -> None:
    later = _add(db, status="pending")
    first = _add(db, status="delivered")
    assert [r["id"] for r in pending_rows(db)][:1] == [first]
    assert later in [r["id"] for r in pending_rows(db)]


def test_物件ページ由来はそのまま写す(db) -> None:
    """経路B（listing_sources）は source_url が販売ページ。読み直す必要が無い。"""
    source = next(iter(CONFIG.listing_sources), None)
    if source is None:
        pytest.skip("listing_sources が設定されていません")
    key = source.key
    url = "https://example.com/listing/1"
    property_id = _add(db, source=key, url=url)

    client = FakeClient()
    stats = relink(CONFIG, db, client=client)
    assert stats.copied == 1
    assert client.urls == []          # 販売サイトへ取りに行かない
    row = db.execute("SELECT listing_url FROM properties WHERE id = ?",
                     (property_id,)).fetchone()
    assert row["listing_url"] == url


def test_仲介の読み物は選ばない() -> None:
    """Corcoran の市況レポートを拾っていた（2026-08-22 の実測）。

    ドメインは仲介、パスに年号の数字もあるので、深さと数字だけでは
    物件ページと区別が付かない。
    """
    assert signals.pick_listing_url([
        "https://inhabit.corcoran.com/nyc-residential-rental-market-report-june-2026",
    ]) is None


def test_読み物より物件ページを選ぶ() -> None:
    found = signals.pick_listing_url([
        "https://inhabit.corcoran.com/nyc-market-report-june-2026",
        "https://www.corcoran.com/listing/for-sale/205-clinton-st/1234567/regionId/1",
    ])
    assert found is not None and "/listing/" in found
