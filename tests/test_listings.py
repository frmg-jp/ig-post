"""経路A（販売ソースの sitemap 収集）の検証。

実サイトには一切アクセスしない。HttpClient の代わりに、応答を辞書で
持つだけのスタブを差し込む。
"""

from __future__ import annotations

import pytest

from freming.collect.listings import (
    ListingCollector,
    _first_price,
    find_address,
    pick_address,
    pick_tw_address,
)
from freming.config import ListingCrawl, ListingSource, load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import exists_source_url

CONFIG = load_config("config.yaml")


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    return connect(path)


class StubResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class StubClient:
    """URL → 本文 の辞書を返すだけのクライアント。

    どのURLを何回取りに行ったかを記録する。無駄なアクセスをしていないか
    （物件URLでないものを取得していないか）を検証に使う。
    """

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str):
        self.requested.append(url)
        if url not in self.pages:
            raise RuntimeError(f"未登録のURL: {url}")
        return StubResponse(self.pages[url])


def _sitemap(*urls: str) -> str:
    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0"?><urlset>{body}</urlset>'


def _detail(address: str, price: str) -> str:
    return f"""
    <html><head><meta property="og:title" content="{address}">
    <meta property="og:image" content="https://example.com/photo.jpg"></head>
    <body><h2>{address}</h2><div class="price">{price}</div>
    <p>A well kept home with original details throughout the interior.</p>
    </body></html>
    """


def _source(**crawl_kwargs) -> ListingSource:
    return ListingSource(
        key="stub",
        name="Stub Realty",
        rank="B",
        mode="crawl",
        enabled=True,
        base_url="https://stub.example.com",
        crawl=ListingCrawl(**crawl_kwargs),
    )


# --- 住所と価格の取り出し ---------------------------------------------


def test_address_without_comma_before_state() -> None:
    """Dream Town の og:title 形式（州の前にカンマが無い）。"""
    found = find_address("11012 S Kilpatrick Avenue #2NE, Oak Lawn IL 60453")
    assert found == ("11012 S Kilpatrick Avenue #2NE, Oak Lawn, IL 60453", "Oak Lawn")


def test_address_with_comma_before_state() -> None:
    """Vanguard の本文形式（州の前にカンマがある）。"""
    found = find_address(None, "... Contact 95 Highland Way, Inverness, CA 94937 $500,000 Courtesy of")
    assert found is not None
    assert found[1] == "Inverness"


def test_address_takes_the_first_source_that_matches() -> None:
    """og:title を先に見る。ページ全体には他物件の住所が混ざりうる。"""
    found = find_address(
        "1 Main Street, Springfield IL 62701",
        "9 Other Road, Elsewhere CA 90210",
    )
    assert found is not None and found[1] == "Springfield"


def test_address_not_found_returns_none() -> None:
    assert find_address("Rental Listings | Stub Realty", "no address here") is None


def test_company_address_in_the_footer_is_not_used() -> None:
    """会社の住所を物件の所在地にしない。

    実例（2026-08-03）: Nest Seekers はカナダ Wasaga Beach の物件に
    「New York」、Beverly Hills Estates は Bel Air の物件に
    「West Hollywood」が付いた。どちらもフッターの自社住所。
    URLに現れる市名と突き合わせて選び分ける。
    """
    picked = pick_address(
        "https://x.example.com/properties/983-lakewood-rd-new-castle-pa-123456",
        None,
        "983 Lakewood Road, New Castle, PA 16105 ... 505 Fifth Avenue, New York, NY 10017",
    )
    assert picked is not None and picked[1] == "New Castle"


def test_address_is_left_empty_when_it_cannot_be_decided() -> None:
    """どれが物件の住所か決められないときは空にする。誤った所在地を入れない。"""
    picked = pick_address(
        "https://x.example.com/listing/11201-chalon-rd/",
        None,
        "8878 Sunset Blvd, West Hollywood, CA 90069 ... 1 Other Way, Malibu, CA 90265",
    )
    assert picked is None


def test_single_address_is_used_even_without_a_url_hint() -> None:
    """住所が1つしか無ければ、会社のものと紛れようがないので使う。"""
    picked = pick_address(
        "https://x.example.com/properties/abc-123456",
        None,
        "Featured listing 42 Elm Street, Springfield, IL 62701 today",
    )
    assert picked is not None and picked[1] == "Springfield"


def test_title_address_wins_over_the_page() -> None:
    """og:title に住所があればそれを使う（住所だけが入っているので確実）。"""
    picked = pick_address(
        "https://x.example.com/properties/9-9-9",
        "5 Oak Street, Oak Lawn IL 60453",
        "999 Company Plaza, Chicago, IL 60601",
    )
    assert picked is not None and picked[1] == "Oak Lawn"


def test_first_price_takes_the_earliest_match() -> None:
    text = "Offered at $1,250,000. Estimated taxes $12,400."
    assert _first_price(text, [r"\$\s?\d[\d,]{4,}"]) == "$1,250,000"


# --- URLの絞り込み -----------------------------------------------------


def test_detail_url_include_rejects_index_pages() -> None:
    """一覧ページを物件と取り違えない。

    実例（2026-08-03、Vanguard）: /properties/[^/]+$ で通していたため
    /properties/commercial のような一覧ページを取得し、ページ内の最高額
    $15,500,000 を物件価格として登録しかけた。
    """
    crawl = ListingCrawl(detail_url_include=[r"/properties/[^/]*-\d{6,}$"])
    assert crawl.detail_allowed("https://x.example.com/properties/95-highland-ca-325100256")
    assert not crawl.detail_allowed("https://x.example.com/properties/commercial")


def test_detail_url_exclude_wins_over_include() -> None:
    crawl = ListingCrawl(
        detail_url_include=[r"/properties/"], detail_url_exclude=[r"/sold"]
    )
    assert not crawl.detail_allowed("https://x.example.com/properties/sold/123")


# --- sitemap のたどり方 ------------------------------------------------


def test_nested_sitemap_is_followed_until_listings_appear(db) -> None:
    client = StubClient(
        {
            "https://stub.example.com/sitemap.xml": _sitemap(
                "https://stub.example.com/sitemap-static.xml",
                "https://stub.example.com/sitemap-props.xml",
            ),
            "https://stub.example.com/sitemap-static.xml": _sitemap(
                "https://stub.example.com/about"
            ),
            "https://stub.example.com/sitemap-props.xml": _sitemap(
                "https://stub.example.com/properties/1-main-st-123456"
            ),
            "https://stub.example.com/properties/1-main-st-123456": _detail(
                "1 Main St, Springfield IL 62701", "$300,000"
            ),
        }
    )
    source = _source(
        sitemap_urls=["https://stub.example.com/sitemap.xml"],
        detail_url_include=[r"/properties/[^/]*-\d{6,}$"],
    )
    stats = ListingCollector(CONFIG, client, db).collect(source)

    assert stats.inserted == 1
    # 物件でないURL（/about）は取得しない。相手サイトへの無駄なアクセスを増やさない。
    assert "https://stub.example.com/about" not in client.requested


def test_pages_without_a_price_are_not_registered(db) -> None:
    """価格の無いページは候補にしない（売却済み・準備中・一覧の取り違え）。"""
    client = StubClient(
        {
            "https://stub.example.com/sitemap.xml": _sitemap(
                "https://stub.example.com/properties/2-oak-st-222222"
            ),
            "https://stub.example.com/properties/2-oak-st-222222": (
                "<html><body><p>Coming soon.</p></body></html>"
            ),
        }
    )
    source = _source(
        sitemap_urls=["https://stub.example.com/sitemap.xml"],
        detail_url_include=[r"/properties/[^/]*-\d{6,}$"],
    )
    stats = ListingCollector(CONFIG, client, db).collect(source)

    assert stats.inserted == 0
    assert stats.no_price == 1
    assert stats.no_price_samples == ["https://stub.example.com/properties/2-oak-st-222222"]


# --- 登録の中身と重複防止 ---------------------------------------------


def test_listing_is_registered_as_for_sale_with_price_and_city(db) -> None:
    """物件ページは売出中であることが前提。販売シグナルの推定を通さない。"""
    url = "https://stub.example.com/properties/3-elm-st-333333"
    client = StubClient(
        {
            "https://stub.example.com/sitemap.xml": _sitemap(url),
            url: _detail("3 Elm St, Oak Lawn IL 60453", "$450,000"),
        }
    )
    source = _source(
        sitemap_urls=["https://stub.example.com/sitemap.xml"],
        detail_url_include=[r"/properties/[^/]*-\d{6,}$"],
    )
    ListingCollector(CONFIG, client, db).collect(source)

    row = db.execute(
        "SELECT price, location_city, location_country, is_for_sale, signal_score,"
        " for_sale_evidence, thumbnail_url FROM properties WHERE source_url = ?",
        (url,),
    ).fetchone()
    assert row["price"] == "$450,000"
    assert row["location_city"] == "Oak Lawn"
    assert row["location_country"] == "United States"
    assert row["is_for_sale"] == 1
    # 経路Bのようなシグナル点は付けない。根拠は「物件ページであること」。
    assert row["signal_score"] is None
    assert "Stub Realty" in row["for_sale_evidence"]
    assert row["thumbnail_url"]


def test_known_urls_are_not_fetched_again(db) -> None:
    """既に登録済みの物件ページは取得しない。再実行のたびに相手を叩かない。"""
    url = "https://stub.example.com/properties/4-ash-st-444444"
    pages = {
        "https://stub.example.com/sitemap.xml": _sitemap(url),
        url: _detail("4 Ash St, Chicago IL 60605", "$600,000"),
    }
    source = _source(
        sitemap_urls=["https://stub.example.com/sitemap.xml"],
        detail_url_include=[r"/properties/[^/]*-\d{6,}$"],
    )
    ListingCollector(CONFIG, StubClient(pages), db).collect(source)
    assert exists_source_url(db, url)

    again = StubClient(pages)
    stats = ListingCollector(CONFIG, again, db).collect(source)
    assert stats.skipped_known == 1
    assert stats.fetched == 0
    assert url not in again.requested


def test_max_details_caps_the_number_of_pages_fetched(db) -> None:
    """sitemap は数万件ある。上限が効かないと3秒間隔で何時間も走り続ける。"""
    urls = [f"https://stub.example.com/properties/{n}-pine-st-{n:06d}" for n in range(1, 6)]
    pages = {"https://stub.example.com/sitemap.xml": _sitemap(*urls)}
    for n, u in enumerate(urls, start=1):
        pages[u] = _detail(f"{n} Pine St, Chicago IL 60605", f"${n}00,000")
    source = _source(
        sitemap_urls=["https://stub.example.com/sitemap.xml"],
        detail_url_include=[r"/properties/[^/]*-\d{6,}$"],
        max_details=2,
    )
    stats = ListingCollector(CONFIG, StubClient(pages), db).collect(source)
    assert stats.matched_urls == 5
    assert stats.fetched == 2
    assert stats.inserted == 2


def test_dry_run_does_not_write(db) -> None:
    url = "https://stub.example.com/properties/5-fir-st-555555"
    client = StubClient(
        {
            "https://stub.example.com/sitemap.xml": _sitemap(url),
            url: _detail("5 Fir St, Chicago IL 60605", "$700,000"),
        }
    )
    source = _source(
        sitemap_urls=["https://stub.example.com/sitemap.xml"],
        detail_url_include=[r"/properties/[^/]*-\d{6,}$"],
    )
    stats = ListingCollector(CONFIG, client, db).collect(source, dry_run=True)
    assert stats.inserted == 0
    assert not exists_source_url(db, url)


# --- 設定の取り違え防止 -----------------------------------------------


def test_manual_only_sources_are_never_crawled() -> None:
    """手動投入のみのソースを自動収集に回さない。"""
    from freming.collect.listings import collect_listing_source

    with pytest.raises(SystemExit) as exc:
        collect_listing_source(CONFIG, "zillow")
    assert "mode: manual_only" in str(exc.value)


# --- 所在地の分からない物件を入れない -----------------------------------
#
# エリアはスコアの2割を占める軸で、空だと採点が成り立たない。
# pick_address は会社の住所を掴むくらいなら None を返す作りなので、
# 「決められなかったもの」が所在地不明のまま入っていた。


def _ambiguous(price: str) -> str:
    """物件の住所を特定できないページ。

    会社の住所と他物件の住所が並び、URLの slug にどちらの市名も出ない。
    pick_address はこの状況で None を返す。

    写真はあることにしておく。ここで見たいのは所在地の判定なので、
    写真の有無で結果が変わらないようにする。
    """
    return f"""
    <html><head><meta property="og:title" content="Charming home">
    <meta property="og:image" content="https://example.com/photo.jpg"></head>
    <body><h2>Charming home</h2><div class="price">{price}</div>
    <footer>1 Office Plaza, West Hollywood CA 90069</footer>
    <div>Nearby: 9 Other Road, Elsewhere CA 90210</div>
    <p>A well kept home.</p></body></html>
    """


def test_a_listing_without_a_location_is_skipped(db) -> None:
    url = "https://stub.example.com/p/900001"
    source = _source(
        index_urls=["https://stub.example.com/list"],
        detail_url_include=[r"/p/\d+"],
        price_patterns=[r"\$\s?\d[\d,]{4,}"],
    )
    pages = {
        "https://stub.example.com/list": f'<html><body><a href="{url}">物件</a></body></html>',
        url: _ambiguous("$1,250,000"),
    }
    stats = ListingCollector(CONFIG, StubClient(pages), db).collect(source)

    assert stats.fetched == 1
    assert stats.inserted == 0
    assert stats.no_location == 1
    assert stats.no_location_samples == [url]
    assert not exists_source_url(db, url)


def test_a_listing_with_a_location_still_goes_in(db) -> None:
    """歯止めが効きすぎていないこと。住所が取れるものは今まで通り入る。"""
    url = "https://stub.example.com/p/900002"
    source = _source(
        index_urls=["https://stub.example.com/list"],
        detail_url_include=[r"/p/\d+"],
        price_patterns=[r"\$\s?\d[\d,]{4,}"],
    )
    pages = {
        "https://stub.example.com/list": f'<html><body><a href="{url}">物件</a></body></html>',
        url: _detail("11012 S Kilpatrick Avenue, Oak Lawn IL 60453", "$1,250,000"),
    }
    stats = ListingCollector(CONFIG, StubClient(pages), db).collect(source)

    assert stats.inserted == 1
    assert stats.no_location == 0


def test_require_location_can_be_turned_off(db) -> None:
    """国しか分からないサイト（台湾など）向けの逃げ道。

    location_from: none のソースは所在地を取らないので、既定のままだと
    1件も入らなくなる。
    """
    url = "https://stub.example.com/p/900003"
    source = _source(
        index_urls=["https://stub.example.com/list"],
        detail_url_include=[r"/p/\d+"],
        price_patterns=[r"\$\s?\d[\d,]{4,}"],
        location_from="none",
        require_location=False,
        country="Taiwan",
    )
    pages = {
        "https://stub.example.com/list": f'<html><body><a href="{url}">物件</a></body></html>',
        url: _ambiguous("$1,250,000"),
    }
    stats = ListingCollector(CONFIG, StubClient(pages), db).collect(source)

    assert stats.inserted == 1
    row = db.execute(
        "SELECT location_city, location_country FROM properties WHERE source_url = ?", (url,)
    ).fetchone()
    assert row["location_city"] is None
    assert row["location_country"] == "Taiwan"


# --- 台湾の住所 ---------------------------------------------------------
#
# 「住所が漢字だから取れない」は誤り。台湾の住所は 縣市＋區 が定型で、
# 米国住所と同じように錨にできる。実測した2サイトの実物で確かめる。


def test_taiwan_address_from_og_title() -> None:
    """住商不動產の og:title は先頭が住所。"""
    found = pick_tw_address("台北市中山區台北市中正國小旁靜巷雅寓 - 住商不動產", "")
    assert found == ("台北市中山區", "台北市")


def test_taiwan_address_from_the_label() -> None:
    """21世紀不動產は og:title に住所が無く、本文の「地址」の後にある。"""
    found = pick_tw_address(
        "㊝大明方正邊間三樓 - 21世紀不動產",
        "類型/現況 公寓 [住宅] 地址 新北市板橋區大明街 坪數 28.96 坪",
    )
    assert found == ("新北市板橋區", "新北市")


def test_taiwan_county_seat_city_is_matched() -> None:
    """縣轄市（新竹縣竹北市）。第2階層が 市 で終わる形も拾う。"""
    found = pick_tw_address(None, "地址：新竹縣竹北市光明六路")
    assert found == ("新竹縣竹北市", "新竹縣")


def test_taiwan_traditional_form_is_normalised() -> None:
    """臺 と 台 が混ざる。同じ市が2通りでDBに入らないよう寄せる。"""
    assert pick_tw_address("臺北市信義區の物件", "") == ("台北市信義區", "台北市")


def test_the_company_address_without_a_district_is_not_matched() -> None:
    """住商のフッター「台北市敦化南路二段267號」は 區 が無いので掛からない。

    これが効くから「ページ全体で1種類だけ」の判定が使える。
    """
    assert pick_tw_address(None, "地址：台北市敦化南路二段267號3F之2 電話：0800-212-306") is None


def test_two_different_taiwan_cities_are_refused() -> None:
    """どれが物件の住所か決められないときは入れない。"""
    assert pick_tw_address(None, "台北市中山區の物件 / 高雄市苓雅區の物件") is None


def test_taiwan_listing_gets_a_city(db) -> None:
    url = "https://stub.example.com/p/700001"
    source = _source(
        index_urls=["https://stub.example.com/list"],
        detail_url_include=[r"/p/\d+"],
        price_patterns=[r"\d[\d,]*(?:\.\d+)?\s*(?:萬元|萬|億)(?!\s*/\s*坪)"],
        location_from="tw_address",
        country="Taiwan",
    )
    detail = """
    <html><head><meta property="og:title" content="台北市中山區靜巷雅寓">
    <meta property="og:image" content="https://example.com/photo.jpg"></head>
    <body><div>單價 55 萬 / 坪</div><div>總價 3,980 萬</div>
    <p>採光良好。</p>
    <footer>地址：台北市敦化南路二段267號3F之2</footer></body></html>
    """
    pages = {
        "https://stub.example.com/list": f'<html><body><a href="{url}">物件</a></body></html>',
        url: detail,
    }
    stats = ListingCollector(CONFIG, StubClient(pages), db).collect(source)

    assert stats.inserted == 1
    row = db.execute(
        "SELECT price, location_city, location_country FROM properties WHERE source_url = ?",
        (url,),
    ).fetchone()
    # 坪単価（55 萬 / 坪）ではなく総額を取る
    assert row["price"] == "3,980 萬"
    assert row["location_city"] == "台北市"
    assert row["location_country"] == "Taiwan"


# --- 写真の歯止め -------------------------------------------------------
#
# 物件情報サイトは写真が未登録の物件にも og:image を返す。中身は単色の板で
# 寸法は本物と同じ（Dream Town は 1280x800 の #D0D0D0）ため、
# images.min_short_edge_px では落ちない。審査UIに灰色の四角が並んだ回帰。


def _photo_bytes(colour) -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    if colour == "noise":
        image = Image.effect_noise((512, 512), 60).convert("RGB")
    else:
        image = Image.new("RGB", (1280, 800), colour)
    image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


class PhotoStubClient(StubClient):
    """本文に加えて、画像URL → バイト列 も返せるクライアント。"""

    def __init__(self, pages: dict[str, str], photos: dict[str, bytes]) -> None:
        super().__init__(pages)
        self.photos = photos

    def get(self, url: str):
        if url in self.photos:
            self.requested.append(url)
            response = StubResponse("")
            response.content = self.photos[url]
            return response
        return super().get(url)


def _with_photo(address: str, price: str, photo_url: str) -> str:
    return f"""
    <html><head><meta property="og:title" content="{address}">
    <meta property="og:image" content="{photo_url}"></head>
    <body><h2>{address}</h2><div class="price">{price}</div>
    <p>A well kept home with original details throughout.</p></body></html>
    """


def _photo_case(db, photo: bytes):
    url = "https://stub.example.com/p/800001"
    photo_url = "https://photos.example.com/1.jpg"
    source = _source(
        index_urls=["https://stub.example.com/list"],
        detail_url_include=[r"/p/\d+"],
        price_patterns=[r"\$\s?\d[\d,]{4,}"],
    )
    pages = {
        "https://stub.example.com/list": f'<html><body><a href="{url}">物件</a></body></html>',
        url: _with_photo("11012 S Kilpatrick Avenue, Oak Lawn IL 60453", "$170,000", photo_url),
    }
    client = PhotoStubClient(pages, {photo_url: photo})
    return url, ListingCollector(CONFIG, client, db).collect(source)


def test_placeholder_photo_keeps_the_listing_out(db) -> None:
    """実測した Dream Town のプレースホルダと同じ単色画像。"""
    url, stats = _photo_case(db, _photo_bytes((208, 208, 208)))

    assert stats.inserted == 0
    assert stats.no_photo == 1
    assert stats.no_photo_samples == [url]
    assert not exists_source_url(db, url)


def test_a_real_photo_still_goes_in(db) -> None:
    """歯止めが効きすぎていないこと。"""
    url, stats = _photo_case(db, _photo_bytes("noise"))

    assert stats.inserted == 1
    assert stats.no_photo == 0
    assert exists_source_url(db, url)


def test_a_listing_without_any_image_is_skipped(db) -> None:
    """og:image そのものが無いページも入れない。"""
    url = "https://stub.example.com/p/800002"
    source = _source(
        index_urls=["https://stub.example.com/list"],
        detail_url_include=[r"/p/\d+"],
        price_patterns=[r"\$\s?\d[\d,]{4,}"],
    )
    pages = {
        "https://stub.example.com/list": f'<html><body><a href="{url}">物件</a></body></html>',
        url: """
        <html><head><meta property="og:title" content="11012 S Kilpatrick Avenue, Oak Lawn IL 60453"></head>
        <body><div class="price">$170,000</div><p>A well kept home.</p></body></html>
        """,
    }
    stats = ListingCollector(CONFIG, StubClient(pages), db).collect(source)

    assert stats.inserted == 0
    assert stats.no_photo == 1


def test_image_fetch_failure_does_not_drop_the_listing(db) -> None:
    """画像が取れないだけでは落とさない。相手の一時的な不調で候補が消えるほうが害が大きい。"""
    url = "https://stub.example.com/p/800003"
    source = _source(
        index_urls=["https://stub.example.com/list"],
        detail_url_include=[r"/p/\d+"],
        price_patterns=[r"\$\s?\d[\d,]{4,}"],
    )
    pages = {
        "https://stub.example.com/list": f'<html><body><a href="{url}">物件</a></body></html>',
        # og:image は指しているが、StubClient に登録が無いので取得は失敗する
        url: _with_photo(
            "11012 S Kilpatrick Avenue, Oak Lawn IL 60453", "$170,000",
            "https://photos.example.com/missing.jpg",
        ),
    }
    stats = ListingCollector(CONFIG, StubClient(pages), db).collect(source)

    assert stats.inserted == 1
    assert stats.no_photo == 0
