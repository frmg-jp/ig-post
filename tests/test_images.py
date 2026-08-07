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
from freming.images.placeholder import is_flat_image
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


def _gif(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (120, 110, 100)).save(buffer, "GIF")
    return buffer.getvalue()


def test_wrong_format_is_skipped(config, conn, row) -> None:
    """許可していない形式は落とす。判定は中身から行う。"""
    pages = _pages()
    # ヘッダは image/jpeg を名乗るが、中身は GIF。中身が優先される。
    pages["https://example.com/photos/a.jpg"] = _Response(_gif(1600, 1200), "image/jpeg")
    stats = fetch_images(config, conn, row, client=FakeClient(pages))

    assert stats.wrong_type == 1
    assert stats.downloaded == 1


def test_a_real_image_served_as_octet_stream_is_accepted(config, conn, row) -> None:
    """配信側が Content-Type を付けていなくても、中身が正しければ使う。

    実測（2026-08-07、Coldwell Banker）では3枚中2枚が
    application/octet-stream で返り、ヘッダを信じると正しいJPEGを
    「形式外」で捨てていた。
    """
    pages = _pages()
    pages["https://example.com/photos/a.jpg"] = _Response(
        _png(1600, 1200), "application/octet-stream"
    )
    stats = fetch_images(config, conn, row, client=FakeClient(pages))

    assert stats.wrong_type == 0
    assert stats.downloaded == 2


def test_data_that_is_not_an_image_is_still_refused(config, conn, row) -> None:
    """形式の判定を中身に移しても、非画像は通さない。"""
    pages = _pages()
    pages["https://example.com/photos/a.jpg"] = _Response(b"not an image", "image/jpeg")
    stats = fetch_images(config, conn, row, client=FakeClient(pages))

    assert stats.failed == 1
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


def test_same_photo_in_different_sizes_is_taken_once() -> None:
    """同じ写真のサイズ違いを2枚採らないこと。

    WordPress系のサイトでは og:image が原寸、本文中がリサイズ版になり、
    URL文字列は違うのに同じ写真、ということが起きる。実際に 01.jpg と
    02.jpg が同じ写真になった。
    """
    html = """
    <html><head>
      <meta property="og:image" content="/wp-content/uploads/2026/01/hero.jpg">
    </head><body><article>
      <img src="/wp-content/uploads/2026/01/hero-1600x1067.jpg">
      <img src="/wp-content/uploads/2026/01/interior.jpg">
    </article></body></html>
    """
    urls = extract_image_urls(html, "https://www.6sqft.com/post/")
    assert urls == [
        "https://www.6sqft.com/wp-content/uploads/2026/01/hero.jpg",
        "https://www.6sqft.com/wp-content/uploads/2026/01/interior.jpg",
    ]


def test_scaled_and_retina_variants_are_deduped() -> None:
    """-scaled や @2x も同じ写真の別表現として扱うこと。"""
    html = """
    <article>
      <img src="/uploads/facade.jpg">
      <img src="/uploads/facade-scaled.jpg">
      <img src="/uploads/facade@2x.jpg">
      <img src="/uploads/facade.jpg?v=2">
    </article>
    """
    urls = extract_image_urls(html, "https://example.com/")
    assert urls == ["https://example.com/uploads/facade.jpg"]


def test_numbered_photos_are_not_merged() -> None:
    """連番の別写真まで同一視しないこと（-1 と -2 は別の写真）。"""
    html = """
    <article>
      <img src="/uploads/room-1.jpg"><img src="/uploads/room-2.jpg">
    </article>
    """
    urls = extract_image_urls(html, "https://example.com/")
    assert len(urls) == 2


def test_clear_images_allows_refetching(config, conn, row) -> None:
    """抽出ルールを直したあと、取得済みを捨てて取り直せること。"""
    from freming.db.repository import clear_images

    fetch_images(config, conn, row, client=FakeClient(_pages()))
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM images WHERE property_id = ?", (row["id"],)
    ).fetchone()["n"] == 2

    # 返す件数は「消した画像＋消した除外記録」。除外記録しか無い物件で
    # 0 を返すと、呼び出し側が「対象なし」と誤って報告する。
    removed = clear_images(conn, int(row["id"]))
    assert removed == 2 + 1
    # 弾いたURLの記録も消える（消さないと同じ判定が繰り返される）
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM image_skips WHERE property_id = ?", (row["id"],)
    ).fetchone()["n"] == 0

    second = FakeClient(_pages())
    stats = fetch_images(config, conn, row, client=second)
    assert stats.downloaded == 2


def test_clear_images_refuses_delivered(config, conn, row) -> None:
    """納品済みは取り直せない（Drive の中身と食い違うため）。"""
    from freming.db.repository import clear_images

    fetch_images(config, conn, row, client=FakeClient(_pages()))
    conn.execute("UPDATE properties SET status = 'delivered' WHERE id = ?", (row["id"],))
    conn.commit()

    assert clear_images(conn, int(row["id"])) == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM images WHERE property_id = ?", (row["id"],)
    ).fetchone()["n"] == 2


def test_saved_filename_ignores_query_string() -> None:
    """CDN配信のクエリ付きURLでも、まともなファイル名で保存すること。

    6sqft は photo.jpg?w=2000&format=webp の形で配信する。URL全体から
    拡張子を取ると .jpg?w=2000&format=webp がファイル名になっていた。
    """
    from freming.images.fetch import _suffix_of

    assert _suffix_of("https://x.com/a/photo.jpg?w=2000&format=webp") == ".jpg"
    assert _suffix_of("https://x.com/a/photo.PNG") == ".png"
    assert _suffix_of("https://x.com/a/photo.webp?v=1") == ".webp"
    # 想定外の拡張子は .jpg に寄せる（中身はどのみち再エンコードする）
    assert _suffix_of("https://x.com/a/photo") == ".jpg"
    assert _suffix_of("https://x.com/a/photo.php?id=3") == ".jpg"


def test_downloaded_files_have_clean_names(config, conn, row) -> None:
    pages = {
        ARTICLE_URL: _Response(
            '<article><img src="/photos/a.jpg?w=2000&format=webp"></article>', "text/html"
        ),
        "https://example.com/photos/a.jpg?w=2000&format=webp": _Response(
            _png(1600, 1200), "image/jpeg"
        ),
    }
    fetch_images(config, conn, row, client=FakeClient(pages))

    saved = conn.execute(
        "SELECT local_path FROM images WHERE property_id = ?", (row["id"],)
    ).fetchone()
    assert Path(saved["local_path"]).name == "01.jpg"


def test_refetch_reports_what_is_already_on_hand(config, conn, tmp_path) -> None:
    """再実行時に「採用 0 枚」だけを出すと、失敗したように読める。

    dry-run の直後に本実行すると全URLが取得済みになる。実際は手元に
    10枚あって加工もできているのに、ログが 0 枚と言っていた。
    """
    from freming.images.fetch import FetchStats

    stats = FetchStats(property_id=1, found_urls=25, downloaded=0, already_have=10)
    assert stats.usable == 10
    text = stats.summary()
    assert "手元 10 枚" in text
    assert "新規 0" in text
    assert "取得済み 10" in text


# ----------------------------------------------------------------------
# 代表画像が合成のメディア（2026-08-03 Robb Report）
#
# セレブ記事の代表画像が「物件写真に人物の顔写真を丸く重ねた合成画像」に
# なっている。そのままだと審査UIのサムネイルも納品の 01.jpg もその合成に
# なるので、先頭を飛ばして2枚目から使う。
# ----------------------------------------------------------------------
_COMPOSITE_ARTICLE = """
<html><head>
  <meta property="og:image" content="/wp/2026/07/lead-composite-1600x900.jpg">
</head><body><article>
  <img src="/wp/2026/07/lead-composite-1024x576.jpg">
  <p>The ranch house sits on two acres.</p>
  <img src="/wp/2026/07/exterior-pool.jpg">
  <img src="/wp/2026/07/living-room.jpg">
</article></body></html>
"""


def _names(urls):
    return [url.rsplit("/", 1)[1] for url in urls]


def test_lead_image_is_kept_by_default() -> None:
    urls = extract_image_urls(_COMPOSITE_ARTICLE, "https://robbreport.com/a")
    assert _names(urls)[0] == "lead-composite-1600x900.jpg"


def test_lead_image_can_be_skipped() -> None:
    urls = extract_image_urls(
        _COMPOSITE_ARTICLE, "https://robbreport.com/a", skip_lead_image=True
    )
    assert _names(urls) == ["exterior-pool.jpg", "living-room.jpg"]


def test_size_variant_of_the_lead_is_not_the_second_image() -> None:
    """og:image と記事の先頭画像は同じ写真のことが多い。

    重複を落としてから数えないと、「2枚目」がまた同じ合成画像になる。
    """
    urls = extract_image_urls(
        _COMPOSITE_ARTICLE, "https://robbreport.com/a", skip_lead_image=True
    )
    assert not any("lead-composite" in name for name in _names(urls))


def test_a_single_image_is_never_dropped() -> None:
    """1枚しかないなら飛ばさない。合成画像でも無いよりはまし。"""
    html = (
        '<html><head><meta property="og:image" content="/only.jpg"></head>'
        "<body><article><p>x</p></article></body></html>"
    )
    urls = extract_image_urls(html, "https://robbreport.com/a", skip_lead_image=True)
    assert _names(urls) == ["only.jpg"]


# ----------------------------------------------------------------------
# 「写真なし」プレースホルダの判定。
#
# 物件情報サイトは写真が未登録の物件にも og:image を返す。中身は単色の
# 板で、寸法は本物の写真と同じことが多い（Dream Town は 1280x800 の
# #D0D0D0）。min_short_edge_px では落ちないので、別の条件として持つ。


def _jpeg(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def test_solid_colour_is_flat() -> None:
    """実測した Dream Town のプレースホルダと同じ条件。"""
    assert is_flat_image(_jpeg(Image.new("RGB", (1280, 800), (208, 208, 208))))


def test_shallow_gradient_is_flat() -> None:
    gradient = Image.linear_gradient("L").convert("RGB").point(lambda v: 200 + v // 32)
    assert is_flat_image(_jpeg(gradient))


def test_photo_like_image_is_not_flat() -> None:
    """階調のある画像は落とさない。"""
    noisy = Image.effect_noise((512, 512), 60).convert("RGB")
    assert not is_flat_image(_jpeg(noisy))


def test_undecodable_data_is_not_flat() -> None:
    """判定できないものは通す。候補を消す判断は確信があるときだけにする。"""
    assert not is_flat_image(b"not an image at all")


def test_threshold_comes_from_argument() -> None:
    noisy = _jpeg(Image.effect_noise((512, 512), 60).convert("RGB"))
    assert is_flat_image(noisy, stddev_max=1000.0)


def test_reset_counts_the_skips_too(config, conn, row) -> None:
    """除外の記録しか無い物件でも「対象なし」と報告しないこと。

    images が0枚でも image_skips が残っていれば、次の納品はそのURLを
    取りに行かない。消しているのに0を返すと、呼び出し側が「失敗した」と
    報告して混乱する（property_id=128 で実際に踏んだ）。
    """
    from freming.db.repository import clear_images

    conn.execute(
        "INSERT INTO image_skips (property_id, source_url, reason, created_at) "
        "VALUES (?, 'https://example.com/photos/a.jpg', 'too_small', '2026-08-07')",
        (row["id"],),
    )
    conn.commit()

    assert clear_images(conn, row["id"]) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM image_skips WHERE property_id = ?", (row["id"],)
    ).fetchone()["n"] == 0


def test_the_new_threshold_accepts_mls_sized_photos(config, conn, row) -> None:
    """MLS由来の写真（1024x683 が上限）が通ること。

    2026-08-07 に min_short_edge_px を 800 → 640 に下げた判断の裏づけ。
    Coldwell Banker は ?width=2048 を投げても 1024x683 を返す。
    """
    assert config.images.min_short_edge_px <= 683

    pages = _pages()
    pages["https://example.com/photos/a.jpg"] = _Response(_png(1024, 683), "image/jpeg")
    stats = fetch_images(config, conn, row, client=FakeClient(pages))

    # 1024x683 が採用されていること（_pages には別に 120x90 が入っていて、
    # そちらは落ちるのが正しい）
    assert (1024, 683) in [(i.width, i.height) for i in stats.images]


def test_logos_are_still_refused(config, conn, row) -> None:
    """閾値を下げても、ロゴ・アイコンの類は落とすこと。"""
    pages = _pages()
    pages["https://example.com/photos/a.jpg"] = _Response(_png(300, 200), "image/jpeg")
    stats = fetch_images(config, conn, row, client=FakeClient(pages))

    # 300x200 と、_pages に元からある 120x90 の2枚が落ちる
    assert stats.too_small == 2
    assert (300, 200) not in [(i.width, i.height) for i in stats.images]


def test_stale_skips_are_cleared_in_bulk(config, conn, row) -> None:
    """基準を変えたあと、既存の物件が復活できること。

    一度弾いたURLは記録が残る限り二度と取りに行かない。閾値を下げても
    既存の物件が復活しないので、記録を落とす経路が要る。
    """
    from freming.db.repository import clear_stale_skips

    for url, reason in (
        ("https://example.com/1.jpg", "too_small"),
        ("https://example.com/2.jpg", "wrong_type"),
        ("https://example.com/3.jpg", "failed"),
    ):
        conn.execute(
            "INSERT INTO image_skips (property_id, source_url, reason, created_at) "
            "VALUES (?, ?, ?, '2026-08-07')",
            (row["id"], url, reason),
        )
    conn.commit()

    assert clear_stale_skips(conn) == 2
    left = conn.execute(
        "SELECT reason FROM image_skips WHERE property_id = ?", (row["id"],)
    ).fetchall()
    # 取得できなかった記録は残す（基準とは無関係なので消す理由がない）
    assert [r["reason"] for r in left] == ["failed"]


def test_delivered_properties_keep_their_skips(config, conn, row) -> None:
    """納品済みは触らない（Drive の中身と食い違う）。"""
    from freming.db.repository import clear_stale_skips

    conn.execute("UPDATE properties SET status = 'delivered' WHERE id = ?", (row["id"],))
    conn.execute(
        "INSERT INTO image_skips (property_id, source_url, reason, created_at) "
        "VALUES (?, 'https://example.com/1.jpg', 'too_small', '2026-08-07')",
        (row["id"],),
    )
    conn.commit()

    assert clear_stale_skips(conn) == 0
