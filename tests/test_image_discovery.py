"""[4] 足りない画像を他のサイトから足す経路のテスト。

ネットワークは使わない。画像検索（Cloud Vision）の応答は差し替え、
ページの取得は FakeClient で受ける。
"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from freming.collect.base import Candidate
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import insert_candidate
from freming.images import discover
from freming.images.discover import (
    MATCH_MIN,
    DiscoveryError,
    PageHit,
    fill_property,
    is_blocked,
    match_score,
    matching_pages,
    search_pages,
    search_query,
    usable_pages,
)
from freming.net.client import RobotsDisallowed

ARTICLE_URL = "https://example.com/maybeck-house/"
OTHER_URL = "https://another.example.org/bernard-maybeck-house/"


def _png(width: int = 900, height: int = 900) -> bytes:
    """写真の代わり。**単色にしない**——単色は「写真なし」板として落ちる。"""
    import random

    rng = random.Random(1)
    image = Image.new("RGB", (width, height))
    image.putdata([
        (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        for _ in range(width * height)
    ])
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _flat_png(width: int, height: int, color=(208, 208, 208)) -> bytes:
    """物件サイトが写真の無い物件に返す単色の板。"""
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


class _Response:
    def __init__(self, content: bytes | str) -> None:
        if isinstance(content, str):
            self.text = content
            self.content = content.encode("utf-8")
        else:
            self.content = content
            self.text = ""
        self.status_code = 200


class FakeClient:
    def __init__(self, pages: dict, disallowed: set[str] | None = None) -> None:
        self.pages = pages
        self.disallowed = disallowed or set()
        self.requested: list[str] = []

    def get(self, url: str, **_kwargs) -> _Response:
        self.requested.append(url)
        if url in self.disallowed:
            raise RobotsDisallowed(url)
        if url not in self.pages:
            raise RuntimeError(f"想定外のURL: {url}")
        return self.pages[url]

    def close(self) -> None:
        pass


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "fill.db"
    cfg.images.work_dir = tmp_path / "images"
    cfg.images.discovery.enabled = True
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


@pytest.fixture()
def row(conn):
    property_id = insert_candidate(
        conn,
        Candidate(
            source="wowhaus", source_rank="A", source_url=ARTICLE_URL,
            title="Bernard Maybeck house", content_text="...", is_for_sale=1,
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM properties WHERE id = ?", (property_id,)
    ).fetchone()


def _have_image(conn, property_id: int, url: str, position: int = 1) -> None:
    conn.execute(
        "INSERT INTO images (property_id, source_url, width, height, position, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (property_id, url, 1200, 800, position, "2026-09-15T00:00:00+00:00"),
    )
    conn.commit()


# --- 開いてよいページの絞り込み ---------------------------------------

def test_blocked_sites_are_never_opened(config) -> None:
    """自動収集が禁止されているサイトは、検索結果に出ても開かない。"""
    cfg = config.images.discovery
    assert is_blocked(cfg, "https://www.zillow.com/homedetails/123/")
    assert is_blocked(cfg, "https://photos.redfin.com/x.jpg")
    assert not is_blocked(cfg, "https://www.dwell.com/article/x")

    pages = usable_pages(
        cfg,
        [PageHit("https://www.zillow.com/homedetails/123/"),
         PageHit("https://www.dwell.com/article/x")],
        known=set(),
    )
    assert [p.url for p in pages] == ["https://www.dwell.com/article/x"]


def test_known_pages_are_not_reopened(config) -> None:
    """掲載ページと、前に開いたページは候補から外す。"""
    pages = usable_pages(
        config.images.discovery,
        [PageHit(ARTICLE_URL), PageHit(ARTICLE_URL + "#photos"), PageHit(OTHER_URL)],
        known={ARTICLE_URL},
    )
    assert [p.url for p in pages] == [OTHER_URL]


def test_one_site_cannot_fill_the_whole_list(config) -> None:
    """同じサイトの関連記事で候補が埋まらないようにする。

    1ドメインから何ページも開くと、1物件でひとつの相手を何度も叩く。
    """
    hits = [PageHit(f"https://one.example.com/a{i}") for i in range(5)]
    hits.append(PageHit("https://two.example.com/b"))
    pages = usable_pages(config.images.discovery, hits, known=set())
    assert len(pages) == 3
    assert sum(1 for p in pages if "one.example.com" in p.url) == 2


def test_page_limit_is_respected(config) -> None:
    config.images.discovery.max_pages_per_property = 2
    hits = [PageHit(f"https://s{i}.example.com/x") for i in range(6)]
    assert len(usable_pages(config.images.discovery, hits, known=set())) == 2


# --- 画像検索の応答 ---------------------------------------------------

def test_matching_pages_reads_the_response(config, monkeypatch) -> None:
    body = {
        "responses": [
            {"webDetection": {"pagesWithMatchingImages": [
                {"url": OTHER_URL, "pageTitle": "The same house"},
                {"url": "https://third.example.net/x"},
            ]}},
            # 1枚が読めなくても残りは使う（画像が消えている・403 など）。
            {"error": {"message": "image cannot be fetched"}},
        ]
    }
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Json(200, body))
    hits = matching_pages(config.images.discovery, ["https://cdn/x.jpg"], token="t")
    assert [h.url for h in hits] == [OTHER_URL, "https://third.example.net/x"]


def test_vision_errors_are_not_swallowed(config, monkeypatch) -> None:
    """APIが有効でない・権限が無いときは、そのまま上げる。

    黙って0件を返すと「見つからなかった」と区別が付かず、有効化を
    忘れたまま毎日空振りする。
    """
    import httpx

    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: _Json(403, {"error": {"message": "Cloud Vision API has not been used"}}),
    )
    with pytest.raises(DiscoveryError, match="Cloud Vision"):
        matching_pages(config.images.discovery, ["https://cdn/x.jpg"], token="t")


def test_search_is_skipped_without_keys(config, monkeypatch) -> None:
    """名前・住所での検索は、鍵が無ければ黙って飛ばす（落とさない）。"""
    monkeypatch.delenv("GOOGLE_CSE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CSE_ID", raising=False)
    assert search_pages(config.images.discovery, "Maybeck house Berkeley") == []


def test_search_query_uses_name_and_address(conn, row) -> None:
    conn.execute(
        "UPDATE properties SET display_name = ?, street_address = ?, location_city = ? "
        "WHERE id = ?", ("Grayoaks", "100 Main St", "Ross", row["id"]),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM properties WHERE id = ?", (row["id"],)
    ).fetchone()
    assert search_query(updated) == "Grayoaks 100 Main St Ross"


# --- 実際に足す -------------------------------------------------------

class _Json:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


def test_images_from_other_sites_keep_their_origin(config, conn, row, monkeypatch) -> None:
    """他サイトから足した画像には、そのページのURLが残ること。

    引用元が1つではなくなるので、印が無いとクレジットを確かめられない。
    """
    _have_image(conn, int(row["id"]), "https://cdn.example.com/own-1.jpg")
    monkeypatch.setattr(
        discover, "matching_pages", lambda *a, **k: [PageHit(OTHER_URL)]
    )
    monkeypatch.setattr(discover, "search_pages", lambda *a, **k: [])

    client = FakeClient({
        OTHER_URL: _Response(
            '<article><img src="https://cdn.other.org/extra-1.jpg">'
            '<img src="https://cdn.other.org/extra-2.jpg"></article>'
        ),
        "https://cdn.other.org/extra-1.jpg": _Response(_png()),
        "https://cdn.other.org/extra-2.jpg": _Response(_png()),
    })

    stats = fill_property(config, conn, row, client=client, token="t")

    assert stats.gained == 2
    rows = conn.execute(
        "SELECT source_url, origin_url FROM images WHERE property_id = ? "
        "ORDER BY position", (row["id"],),
    ).fetchall()
    assert rows[0]["origin_url"] is None          # 元の記事の写真
    assert rows[1]["origin_url"] == OTHER_URL     # 足したぶん
    assert rows[2]["origin_url"] == OTHER_URL


def test_the_same_page_is_not_opened_twice(config, conn, row, monkeypatch) -> None:
    """一度開いたページは、次に走らせたときは開かない。"""
    _have_image(conn, int(row["id"]), "https://cdn.example.com/own-1.jpg")
    monkeypatch.setattr(
        discover, "matching_pages", lambda *a, **k: [PageHit(OTHER_URL)]
    )
    monkeypatch.setattr(discover, "search_pages", lambda *a, **k: [])
    pages = {
        OTHER_URL: _Response('<article><img src="https://cdn.other.org/e1.jpg"></article>'),
        "https://cdn.other.org/e1.jpg": _Response(_png()),
    }

    fill_property(config, conn, row, client=FakeClient(pages), token="t")
    second = FakeClient(pages)
    stats = fill_property(config, conn, row, client=second, token="t")

    assert second.requested == []
    assert stats.pages_opened == 0


def test_full_properties_are_left_alone(config, conn, row, monkeypatch) -> None:
    """上限まであるものには、画像検索そのものを呼ばない（＝課金しない）。"""
    for position in range(1, config.images.max_per_property + 1):
        _have_image(conn, int(row["id"]), f"https://cdn.example.com/{position}.jpg", position)

    def _boom(*_a, **_k):  # pragma: no cover - 呼ばれたら失敗
        raise AssertionError("上限まであるのに画像検索を呼んでいる")

    monkeypatch.setattr(discover, "matching_pages", _boom)
    stats = fill_property(config, conn, row, client=FakeClient({}), token="t")
    assert stats.gained == 0
    assert stats.vision_calls == 0


def test_it_stops_at_the_limit(config, conn, row, monkeypatch) -> None:
    """上限を超えて足さない。"""
    config.images.max_per_property = 3
    _have_image(conn, int(row["id"]), "https://cdn.example.com/own-1.jpg")
    monkeypatch.setattr(
        discover, "matching_pages", lambda *a, **k: [PageHit(OTHER_URL)]
    )
    monkeypatch.setattr(discover, "search_pages", lambda *a, **k: [])
    client = FakeClient({
        OTHER_URL: _Response(
            "<article>" + "".join(
                f'<img src="https://cdn.other.org/e{i}.jpg">' for i in range(5)
            ) + "</article>"
        ),
        **{f"https://cdn.other.org/e{i}.jpg": _Response(_png()) for i in range(5)},
    })
    fill_property(config, conn, row, client=client, token="t")
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM images WHERE property_id = ?", (row["id"],)
    ).fetchone()["n"]
    assert count == 3


def test_a_page_that_refuses_does_not_stop_the_rest(config, conn, row, monkeypatch) -> None:
    """robots.txt が許していないページは飛ばし、次の候補へ進む。"""
    _have_image(conn, int(row["id"]), "https://cdn.example.com/own-1.jpg")
    blocked = "https://slow.example.net/a"
    monkeypatch.setattr(
        discover, "matching_pages",
        lambda *a, **k: [PageHit(blocked), PageHit(OTHER_URL)],
    )
    monkeypatch.setattr(discover, "search_pages", lambda *a, **k: [])
    client = FakeClient(
        {
            OTHER_URL: _Response('<article><img src="https://cdn.other.org/e1.jpg"></article>'),
            "https://cdn.other.org/e1.jpg": _Response(_png()),
        },
        disallowed={blocked},
    )
    stats = fill_property(config, conn, row, client=client, token="t")
    assert stats.pages_opened == 1
    assert stats.gained == 1


# --- この物件のページか確かめる -----------------------------------------

def test_unrelated_pages_are_not_opened(conn, row) -> None:
    """**画像検索は「同じ物件だ」とは言っていない。**

    2026-09-15 の初回実行で実際に起きた: property 58（6922 N Owen Avenue）の
    写真をかけたところ Disney の予告編ページが候補に出て、そこに並んでいた
    映画のスチルを3枚取り込んだ。番地も物件名も出てこないページは開かない。
    """
    conn.execute(
        "UPDATE properties SET display_name = ?, street_address = ? WHERE id = ?",
        ("Cape Cod Residence in Edison Park", "6922 N Owen Avenue", row["id"]),
    )
    conn.commit()
    target = conn.execute(
        "SELECT * FROM properties WHERE id = ?", (row["id"],)
    ).fetchone()

    good = PageHit("https://teamfallico.com/properties/12719651/6922-n-owen-avenue/")
    bad = PageHit(
        "https://video.disney.com/watch/the-north-avenue-irregulars-trailer",
        title="The North Avenue Irregulars Trailer",
    )
    assert match_score(target, good) >= MATCH_MIN
    assert match_score(target, bad) < MATCH_MIN


def test_a_distinctive_name_is_enough(conn, row) -> None:
    """住所が無い記事物件は、名前で見分ける（Hezlep House など）。"""
    conn.execute(
        "UPDATE properties SET display_name = ? WHERE id = ?",
        ("Hezlep House", row["id"]),
    )
    conn.commit()
    target = conn.execute(
        "SELECT * FROM properties WHERE id = ?", (row["id"],)
    ).fetchone()
    assert match_score(target, PageHit("https://dwell.com/article/hezlep-house-zook")) >= MATCH_MIN
    # 「house」だけでは通さない。どの物件にも出る語は数えない。
    assert match_score(target, PageHit("https://example.com/a-lovely-house")) < MATCH_MIN


def test_nothing_is_opened_without_a_handle(config, conn, row, monkeypatch) -> None:
    """名前も住所も無ければ、**1ページも開かない**。見分けられないため。"""
    conn.execute(
        "UPDATE properties SET display_name = NULL, title = NULL WHERE id = ?",
        (row["id"],),
    )
    conn.commit()
    target = conn.execute(
        "SELECT * FROM properties WHERE id = ?", (row["id"],)
    ).fetchone()
    _have_image(conn, int(row["id"]), "https://cdn.example.com/own-1.jpg")
    monkeypatch.setattr(discover, "matching_pages", lambda *a, **k: [PageHit(OTHER_URL)])
    monkeypatch.setattr(discover, "search_pages", lambda *a, **k: [])

    client = FakeClient({})
    stats = fill_property(config, conn, target, client=client, token="t")
    assert client.requested == []
    assert stats.gained == 0


def test_undo_removes_only_foreign_images(config, conn, row, monkeypatch) -> None:
    """取り消しは**足した分だけ**。元の記事の写真は残す。"""
    _have_image(conn, int(row["id"]), "https://cdn.example.com/own-1.jpg")
    conn.execute(
        "UPDATE properties SET display_name = ? WHERE id = ?",
        ("Grayoaks", row["id"]),
    )
    conn.commit()
    target = conn.execute(
        "SELECT * FROM properties WHERE id = ?", (row["id"],)
    ).fetchone()
    page = "https://dwell.com/article/grayoaks-maybeck"
    monkeypatch.setattr(discover, "matching_pages", lambda *a, **k: [PageHit(page)])
    monkeypatch.setattr(discover, "search_pages", lambda *a, **k: [])
    client = FakeClient({
        page: _Response('<article><img src="https://cdn.other.org/e1.jpg"></article>'),
        "https://cdn.other.org/e1.jpg": _Response(_png()),
    })
    fill_property(config, conn, target, client=client, token="t")

    removed = discover.undo_fill(conn, int(row["id"]))
    assert [r["origin_url"] for r in removed] == [page]
    left = conn.execute(
        "SELECT source_url FROM images WHERE property_id = ?", (row["id"],)
    ).fetchall()
    assert [r["source_url"] for r in left] == ["https://cdn.example.com/own-1.jpg"]


def test_flat_placeholders_are_not_taken(config, conn, row) -> None:
    """他サイトから足す分は、単色の「写真なし」板を採らない。

    寸法（1280x800）は本物と同じなので、短辺の枠では落ちない。掲載ページ
    から取る分は収集のときに同じ判定を通っているので、ここでは見ない。
    """
    from freming.images.fetch import FetchStats, ingest_urls

    url = "https://cdn.example.com/no-photo.jpg"
    client = FakeClient({url: _Response(_flat_png(1280, 800))})
    stats = ingest_urls(
        config, conn, row, [url], client, FetchStats(property_id=int(row["id"])),
        origin_url="https://other.example.org/listing",
    )
    assert stats.flat == 1
    assert stats.downloaded == 0
