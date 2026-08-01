"""[4][5] 画像の抽出・取得・加工のテスト。

ネットワークは使わず、HTTPクライアントを差し替える。画像は Pillow で
その場で生成する。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from freming.collect.base import Candidate
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import insert_candidate
from freming.images.extract import extract_image_urls
from freming.images.fetch import NoImagesFound, fetch_images
from freming.images.process import process_property_images, to_square
from freming.net.client import RobotsDisallowed

ARTICLE_URL = "https://example.com/warehouse-loft/"


def _png(width: int, height: int, color=(120, 110, 100)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


class _Response:
    def __init__(self, content: bytes | str, content_type: str) -> None:
        if isinstance(content, str):
            self.text = content
            self.content = content.encode("utf-8")
        else:
            self.content = content
            self.text = ""
        self.headers = {"content-type": content_type}
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
    cfg.app.db_path = tmp_path / "img.db"
    cfg.images.work_dir = tmp_path / "images"
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
            title="Warehouse loft", content_text="...", is_for_sale=1,
        ),
    )
    conn.commit()
    return conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()


# --- 抽出 -------------------------------------------------------------

def test_extract_skips_logos_and_icons() -> None:
    """物件写真でない画像はダウンロード前に落とすこと。"""
    html = """
    <html><head><meta property="og:image" content="/hero.jpg"></head>
    <body><header><img src="/assets/logo.png"></header>
    <article>
      <img src="/photos/interior.jpg">
      <img src="/assets/author-avatar.jpg">
      <img src="/photos/facade.jpeg">
      <img src="/share-icon.png">
    </article></body></html>
    """
    urls = extract_image_urls(html, "https://example.com/a/")
    assert urls == [
        "https://example.com/hero.jpg",
        "https://example.com/photos/interior.jpg",
        "https://example.com/photos/facade.jpeg",
    ]


def test_extract_picks_largest_from_srcset() -> None:
    """srcset があれば最大幅を選ぶ（src は小さい版のことが多い）。"""
    html = """
    <article><img src="/small.jpg"
      srcset="/w400.jpg 400w, /w1600.jpg 1600w, /w800.jpg 800w"></article>
    """
    assert extract_image_urls(html, "https://example.com/") == ["https://example.com/w1600.jpg"]


def test_extract_handles_lazy_loading() -> None:
    html = '<article><img data-src="/photos/real.jpg" src="/placeholder.png"></article>'
    assert extract_image_urls(html, "https://example.com/") == ["https://example.com/photos/real.jpg"]


def test_extract_keeps_article_order() -> None:
    """記事に載っている順序を保つ（編集者の並びが1枚目の手がかり）。"""
    html = '<article><img src="/c.jpg"><img src="/a.jpg"><img src="/b.jpg"></article>'
    urls = extract_image_urls(html, "https://example.com/")
    assert [Path(u).name for u in urls] == ["c.jpg", "a.jpg", "b.jpg"]


# --- 取得 -------------------------------------------------------------

def _pages(**extra) -> dict:
    html = """
    <article>
      <img src="/photos/a.jpg"><img src="/photos/b.jpg"><img src="/photos/tiny.jpg">
    </article>
    """
    pages = {
        ARTICLE_URL: _Response(html, "text/html"),
        "https://example.com/photos/a.jpg": _Response(_png(1600, 1200), "image/jpeg"),
        "https://example.com/photos/b.jpg": _Response(_png(1200, 1600), "image/jpeg"),
        "https://example.com/photos/tiny.jpg": _Response(_png(120, 90), "image/jpeg"),
    }
    pages.update(extra)
    return pages


def test_small_images_are_rejected(config, conn, row) -> None:
    """短辺が min_short_edge_px 未満のものは採らない。"""
    stats = fetch_images(config, conn, row, client=FakeClient(_pages()))
    assert stats.downloaded == 2
    assert stats.too_small == 1


def test_downloaded_images_are_recorded(config, conn, row) -> None:
    fetch_images(config, conn, row, client=FakeClient(_pages()))
    rows = conn.execute(
        "SELECT * FROM images WHERE property_id = ? ORDER BY position", (row["id"],)
    ).fetchall()
    assert [r["position"] for r in rows] == [1, 2]
    assert all(Path(r["local_path"]).exists() for r in rows)
    assert rows[0]["width"] == 1600


def test_refetch_does_not_request_known_images(config, conn, row) -> None:
    """再実行で相手サイトへのリクエストを増やさないこと。"""
    fetch_images(config, conn, row, client=FakeClient(_pages()))
    second = FakeClient(_pages())
    fetch_images(config, conn, row, client=second)
    # 記事ページは読むが、取得済みの画像は取りに行かない
    assert second.requested == [ARTICLE_URL]


def test_max_per_property_is_respected(config, conn, row) -> None:
    config.images.max_per_property = 1
    stats = fetch_images(config, conn, row, client=FakeClient(_pages()))
    assert stats.downloaded == 1


def test_wrong_content_type_is_skipped(config, conn, row) -> None:
    pages = _pages()
    pages["https://example.com/photos/a.jpg"] = _Response(b"GIF89a", "image/gif")
    stats = fetch_images(config, conn, row, client=FakeClient(pages))
    assert stats.wrong_type == 1
    assert stats.downloaded == 1


def test_robots_disallow_stops_image_fetch(config, conn, row) -> None:
    """robots.txt が拒否したら取得しない（回避は行わない）。"""
    client = FakeClient(_pages(), disallowed={ARTICLE_URL})
    with pytest.raises(NoImagesFound):
        fetch_images(config, conn, row, client=client)


def test_no_usable_images_raises(config, conn, row) -> None:
    pages = {
        ARTICLE_URL: _Response('<article><img src="/photos/tiny.jpg"></article>', "text/html"),
        "https://example.com/photos/tiny.jpg": _Response(_png(100, 100), "image/jpeg"),
    }
    with pytest.raises(NoImagesFound):
        fetch_images(config, conn, row, client=FakeClient(pages))


# --- 加工 -------------------------------------------------------------

def test_square_output_is_1080(config, tmp_path) -> None:
    source = tmp_path / "in.png"
    source.write_bytes(_png(1600, 1200))
    dest = tmp_path / "out.jpg"

    mode = to_square(source, dest, config.process)

    assert mode == "crop"
    with Image.open(dest) as img:
        assert img.size == (1080, 1080)
        assert img.format == "JPEG"


def test_extreme_aspect_is_padded_not_cropped(config, tmp_path) -> None:
    """パノラマを中央クロップすると建物が切れるので余白で埋める。"""
    source = tmp_path / "pano.png"
    source.write_bytes(_png(3000, 800))          # 縦横比 3.75 > 2.0
    dest = tmp_path / "pano.jpg"

    assert to_square(source, dest, config.process) == "pad"
    with Image.open(dest) as img:
        assert img.size == (1080, 1080)
        # 上下は余白（白）で埋まっている
        assert img.getpixel((540, 5)) == (255, 255, 255)


def test_process_writes_numbered_outputs(config, conn, row) -> None:
    fetch_images(config, conn, row, client=FakeClient(_pages()))
    stats = process_property_images(config, conn, int(row["id"]))

    assert stats.processed == 2
    assert [p.name for p in stats.outputs] == ["01.jpg", "02.jpg"]
    saved = conn.execute(
        "SELECT output_path FROM images WHERE property_id = ? ORDER BY position", (row["id"],)
    ).fetchall()
    assert all(r["output_path"] for r in saved)


def test_missing_source_file_does_not_stop_the_rest(config, conn, row) -> None:
    fetch_images(config, conn, row, client=FakeClient(_pages()))
    first = conn.execute(
        "SELECT * FROM images WHERE property_id = ? ORDER BY position", (row["id"],)
    ).fetchone()
    Path(first["local_path"]).unlink()

    stats = process_property_images(config, conn, int(row["id"]))
    assert stats.failed == 1
    assert stats.processed == 1


def test_rejected_urls_are_remembered(config, conn, row) -> None:
    """一度弾いた画像URLを再実行で取り直さないこと。"""
    fetch_images(config, conn, row, client=FakeClient(_pages()))
    skips = conn.execute(
        "SELECT source_url, reason FROM image_skips WHERE property_id = ?", (row["id"],)
    ).fetchall()
    assert [r["reason"] for r in skips] == ["too_small"]
    assert skips[0]["source_url"].endswith("tiny.jpg")
