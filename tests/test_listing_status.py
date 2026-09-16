"""[1] 販売状況（Listing Status）の確認。

編集方針（WEEKLY GLOBAL ARCHITECTURE REPORT）:

    「現在買える」と記載する物件については、紹介記事だけを根拠にしては
    いけない。記事が存在するだけでは Active と判断しない。

確かめるのは、**推測しないこと**と、**「見ていない」を「販売していない」に
しないこと**。
"""

from __future__ import annotations

import pytest

from freming.collect import listing_status as ls
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.net.client import RobotsDisallowed


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "listing.db"
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


class _Response:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class FakeClient:
    def __init__(self, pages: dict, disallowed: set[str] | None = None) -> None:
        self.pages = pages
        self.disallowed = disallowed or set()
        self.requested: list[str] = []

    def get(self, url: str, **_kwargs) -> _Response:
        self.requested.append(url)
        if url in self.disallowed:
            raise RobotsDisallowed(url)
        return self.pages[url]


def _row(conn, **cols) -> object:
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO properties (source, source_rank, {keys}) "
        f"VALUES ('dwell', 'A', {marks})", tuple(cols.values()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM properties ORDER BY id DESC LIMIT 1").fetchone()


# --- 読み取り ---------------------------------------------------------

def test_構造化データをいちばん信用する() -> None:
    html = '<script type="application/ld+json">{"offers":{"availability":"https://schema.org/SoldOut"}}</script> for sale'
    result = ls.detect(html)
    assert result.value == ls.SOLD
    assert "構造化データ" in result.evidence


def test_状態のラベルを読む() -> None:
    assert ls.detect('<span>Status: Pending</span>').value == ls.PENDING
    assert ls.detect('"listingStatus":"Active"').value == ls.ACTIVE
    assert ls.detect("Listing Status — Withdrawn").value == ls.OFF_MARKET


def test_決まり文句を読む() -> None:
    assert ls.detect("This property is no longer available.").value == ls.OFF_MARKET
    assert ls.detect("Sale pending as of last week").value == ls.PENDING
    assert ls.detect("The home just sold.").value == ls.SOLD


def test_沿革の売却を成約と取り違えない() -> None:
    """**単なる sold は採らない。** 「1998年に売却された」は状態ではない。"""
    html = "The house was sold in 1998 to the architect's family. Now for sale."
    assert ls.detect(html).value != ls.SOLD


def test_書いていなければ読み取れずにする() -> None:
    result = ls.detect("<html><body>A lovely house in the hills.</body></html>")
    assert result.value == ls.UNKNOWN
    assert "見つからない" in result.evidence


def test_MLS番号を拾う() -> None:
    assert ls.find_mls("MLS# 12345678") == "12345678"
    assert ls.find_mls("MLS® Number: SR24098765") == "SR24098765"
    assert ls.find_mls("MLS No. 987654") == "987654"
    # 数字なら何でも拾う、にはしない（価格や郵便番号を拾ってしまう）
    assert ls.find_mls("Listed at $2,400,000 in 60631") is None
    assert ls.find_mls("no number here") is None


# --- 開いてよい相手か -------------------------------------------------

def test_自動収集が禁止のサイトは開かない(config) -> None:
    assert ls.can_check(config, "https://www.dwell.com/article/x") is True
    assert ls.can_check(config, "https://www.zillow.com/homedetails/1/") is False
    assert ls.can_check(config, "https://www.redfin.com/x") is False
    # 手動URL投入だけにしてある販売ソースも同じ扱いにする
    assert ls.can_check(config, "https://www.sothebysrealty.com/x") is False


def test_販売ページを記事より優先する(config, conn) -> None:
    row = _row(
        conn, source_url="https://www.dwell.com/article/x",
        listing_url="https://broker.example.com/listing/1",
    )
    assert ls.check_url(config, row) == "https://broker.example.com/listing/1"


def test_開けない販売ページなら記事に落とす(config, conn) -> None:
    row = _row(
        conn, source_url="https://www.dwell.com/article/x",
        listing_url="https://www.zillow.com/homedetails/1/",
    )
    assert ls.check_url(config, row) == "https://www.dwell.com/article/x"


# --- 1件ぶんの確認 ----------------------------------------------------

def test_確認できなければNoneで返す(config, conn) -> None:
    """**「見ていない」を「販売していない」にしない。**"""
    row = _row(conn, source_url="https://www.zillow.com/homedetails/1/")
    assert ls.check(config, row, client=FakeClient({})) is None


def test_robotsで断られたらNone(config, conn) -> None:
    url = "https://slow.example.com/listing/1"
    row = _row(conn, source_url=url)
    client = FakeClient({}, disallowed={url})
    assert ls.check(config, row, client=client) is None


def test_ページが消えていれば取り下げ(config, conn) -> None:
    url = "https://broker.example.com/listing/1"
    row = _row(conn, source_url=url)
    client = FakeClient({url: _Response("", status_code=404)})
    result = ls.check(config, row, client=client)
    assert result.value == ls.OFF_MARKET
    assert "404" in result.evidence


def test_結果と根拠を残す(config, conn) -> None:
    url = "https://broker.example.com/listing/1"
    row = _row(conn, source_url=url, status="approved")
    client = FakeClient({url: _Response('MLS# 12345678 <b>Status: Sold</b>')})
    result = ls.check(config, row, client=client)
    ls.save(conn, int(row["id"]), result)

    saved = conn.execute(
        "SELECT listing_status, listing_status_at, listing_status_note, mls_number "
        "FROM properties WHERE id = ?", (row["id"],),
    ).fetchone()
    assert saved["listing_status"] == ls.SOLD
    assert saved["listing_status_at"]
    assert "Status: Sold" in saved["listing_status_note"]
    assert saved["mls_number"] == "12345678"


def test_確認できなかった行は触らない(config, conn) -> None:
    row = _row(conn, source_url="https://www.zillow.com/homedetails/1/",
               listing_status="active")
    ls.save(conn, int(row["id"]), None)
    saved = conn.execute(
        "SELECT listing_status FROM properties WHERE id = ?", (row["id"],)
    ).fetchone()
    assert saved["listing_status"] == "active"     # 上書きしない


def test_対象は承認済みと納品済みだけ(config, conn) -> None:
    """未審査まで含めると、出す予定の無い数百件を毎回叩くことになる。"""
    _row(conn, source_url="https://a.example.com/1", status="approved")
    _row(conn, source_url="https://a.example.com/2", status="delivered")
    _row(conn, source_url="https://a.example.com/3", status="pending")
    rows = ls.targets(conn)
    assert len(rows) == 2


def test_未確認のものから先に見る(config, conn) -> None:
    _row(conn, source_url="https://a.example.com/1", status="approved",
         listing_status="active", listing_status_at="2026-09-01T00:00:00+00:00")
    fresh = _row(conn, source_url="https://a.example.com/2", status="approved")
    assert ls.targets(conn)[0]["id"] == fresh["id"]
