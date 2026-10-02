"""[6] 納品のテスト。

Drive API は呼ばず、DriveClient を差し替える。最重要の性質は
「再実行しても重複納品しないこと」。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from freming.collect.base import Candidate
from freming.config import SeriesLabel, load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import insert_candidate
from freming.delivery.deliver import (
    build_meta,
    deliver_approved,
    next_folder_name,
)
from freming.delivery.drive import DriveError

ARTICLE_URL = "https://example.com/warehouse-loft/"


def _png(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (120, 110, 100)).save(buffer, "PNG")
    return buffer.getvalue()


class _Response:
    def __init__(self, content, content_type: str) -> None:
        if isinstance(content, str):
            self.text, self.content = content, content.encode("utf-8")
        else:
            self.content, self.text = content, ""
        self.headers = {"content-type": content_type}


class FakeHttp:
    def __init__(self) -> None:
        self.pages = {
            ARTICLE_URL: _Response(
                '<article><img src="/photos/a.jpg"><img src="/photos/b.jpg"></article>',
                "text/html",
            ),
            "https://example.com/photos/a.jpg": _Response(_png(1600, 1200), "image/jpeg"),
            "https://example.com/photos/b.jpg": _Response(_png(1200, 1600), "image/jpeg"),
        }

    def get(self, url: str, **_kwargs):
        if url not in self.pages:
            raise RuntimeError(f"想定外のURL: {url}")
        return self.pages[url]

    def close(self) -> None:
        pass


class FakeDrive:
    """DriveClient の代わり。作られたフォルダとファイルを記録する。"""

    def __init__(self, fail_on_upload: bool = False) -> None:
        self.folders: dict[str, str] = {}
        self.uploads: list[tuple[str, str]] = []   # (フォルダID, ファイル名)
        self.fail_on_upload = fail_on_upload

    def create_folder(self, name: str, parent_id: str) -> str:
        folder_id = f"folder-{name}"
        self.folders[folder_id] = name
        return folder_id

    def upload_file(self, local_path, name: str, parent_id: str, mime_type: str = ""):
        if self.fail_on_upload:
            raise DriveError("アップロードに失敗しました")
        assert Path(local_path).exists()
        self.uploads.append((parent_id, name))

    def upload_bytes(self, data: bytes, name: str, parent_id: str, mime_type: str = ""):
        self.uploads.append((parent_id, name))


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "deliver.db"
    cfg.images.work_dir = tmp_path / "images"
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


def _approved(conn, url=ARTICLE_URL) -> int:
    property_id = insert_candidate(
        conn,
        Candidate(
            source="wowhaus", source_rank="A", source_url=url,
            title="Warehouse loft", content_text="...", is_for_sale=1,
            price="$2,400,000", location_city="San Francisco",
            location_country="United States",
        ),
    )
    conn.execute(
        "UPDATE properties SET status = 'approved', score = 88.4, "
        "summary = '1868年の倉庫を住居に' WHERE id = ?",
        (property_id,),
    )
    conn.commit()
    return property_id


def test_delivery_uploads_images_and_meta(config, conn) -> None:
    property_id = _approved(conn)
    drive = FakeDrive()

    stats = deliver_approved(config, conn, drive=drive, http=FakeHttp())

    assert len(stats.delivered) == 1
    result = stats.delivered[0]
    assert result.folder_name == "frmg_ig001"
    assert result.image_count == 2
    assert [name for _, name in drive.uploads] == ["01.jpg", "02.jpg", "meta.txt"]
    assert conn.execute(
        "SELECT status FROM properties WHERE id = ?", (property_id,)
    ).fetchone()["status"] == "delivered"


def test_rerun_does_not_deliver_twice(config, conn) -> None:
    """再実行しても重複納品しないこと（要件の中核）。"""
    _approved(conn)
    first = FakeDrive()
    deliver_approved(config, conn, drive=first, http=FakeHttp())

    second = FakeDrive()
    stats = deliver_approved(config, conn, drive=second, http=FakeHttp())

    # status が delivered になっているので対象にすら入らない
    assert stats.delivered == []
    assert second.uploads == []
    assert conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"] == 1


def test_delivery_record_survives_status_reset(config, conn) -> None:
    """status を手で戻しても、納品記録があれば二重納品しないこと。"""
    property_id = _approved(conn)
    deliver_approved(config, conn, drive=FakeDrive(), http=FakeHttp())
    conn.execute("UPDATE properties SET status = 'approved' WHERE id = ?", (property_id,))
    conn.commit()

    drive = FakeDrive()
    stats = deliver_approved(config, conn, drive=drive, http=FakeHttp())

    assert stats.skipped_existing == 1
    assert drive.uploads == []


def test_folder_numbers_increment(config, conn) -> None:
    _approved(conn, "https://example.com/a/")
    _approved(conn, "https://example.com/b/")

    http = FakeHttp()
    http.pages["https://example.com/a/"] = http.pages[ARTICLE_URL]
    http.pages["https://example.com/b/"] = http.pages[ARTICLE_URL]

    stats = deliver_approved(config, conn, drive=FakeDrive(), http=http)
    assert [d.folder_name for d in stats.delivered] == ["frmg_ig001", "frmg_ig002"]


def test_folder_numbers_do_not_reuse_deleted_ones(config, conn) -> None:
    """途中の納品を消しても番号は再利用しない。

    連番はテーブルの件数や最後の行ではなく、記録されている最大値から採る。
    """
    for number, url in ((7, "https://example.com/x/"), (3, "https://example.com/y/")):
        property_id = _approved(conn, url)
        conn.execute(
            "INSERT INTO deliveries (property_id, folder_name, image_count, delivered_at) "
            "VALUES (?, ?, 10, datetime('now'))",
            (property_id, f"frmg_ig{number:03d}"),
        )
    conn.commit()
    assert next_folder_name(conn, config) == "frmg_ig008"


def test_upload_failure_leaves_it_undelivered(config, conn) -> None:
    """途中で落ちたら未納品のままにし、次回やり直せること。"""
    property_id = _approved(conn)
    stats = deliver_approved(
        config, conn, drive=FakeDrive(fail_on_upload=True), http=FakeHttp()
    )

    assert stats.failed == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT status FROM properties WHERE id = ?", (property_id,)
    ).fetchone()["status"] == "approved"


def test_dry_run_touches_neither_drive_nor_db(config, conn) -> None:
    _approved(conn)
    drive = FakeDrive()
    stats = deliver_approved(config, conn, dry_run=True, drive=drive, http=FakeHttp())

    assert len(stats.delivered) == 1
    assert drive.uploads == []
    assert conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"] == 0


def test_property_without_images_is_reported(config, conn) -> None:
    _approved(conn, "https://example.com/empty/")
    http = FakeHttp()
    http.pages["https://example.com/empty/"] = _Response("<article></article>", "text/html")

    stats = deliver_approved(config, conn, drive=FakeDrive(), http=http)
    assert stats.no_images == 1
    assert stats.delivered == []


def test_meta_includes_source_url(config, conn) -> None:
    """出典を必ず残す。あとから素材の出どころを辿れないと使えない。"""
    property_id = _approved(conn)
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    meta = build_meta(row, 10)

    assert f"source_url: {ARTICLE_URL}" in meta
    assert "images: 10" in meta
    assert "1868年の倉庫を住居に" in meta


def test_meta_includes_series_label(config, conn) -> None:
    """meta.txt には key ではなく表示名を書く（人が読むため）。"""
    property_id = _approved(conn)
    conn.execute(
        "UPDATE properties SET series = 'freming_pick' WHERE id = ?", (property_id,)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()

    labelled = config.model_copy(deep=True)
    labelled.series = [SeriesLabel(key="freming_pick", label="FREMING Pick")]
    assert "series: FREMING Pick" in build_meta(row, 10, labelled.series_label(row["series"]))


def test_series_label_falls_back_to_the_key_when_the_series_is_retired(conn) -> None:
    """企画を config から消しても、納品済みの行が表示できなくなることはない。

    2026-08-06 に series を空にした。過去に付けたラベルは残っているので、
    表示名が引けない場合は key をそのまま出す。
    """
    from freming.config import load_config

    retired = load_config("config.yaml")
    assert retired.series == []
    assert retired.series_label("freming_pick") == "freming_pick"

    property_id = _approved(conn)
    conn.execute(
        "UPDATE properties SET series = 'freming_pick' WHERE id = ?", (property_id,)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    assert "series: freming_pick" in build_meta(row, 10, retired.series_label(row["series"]))


def test_meta_without_series_is_blank(config, conn) -> None:
    property_id = _approved(conn)
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    assert "series: \n" in build_meta(row, 10, config.series_label(row["series"]))


# --- 1件の失敗で納品全体を止めない（2026-10-02） -----------------------
# 9/29〜10/02 の4日間、納品が0件だった。承認済みが47件あるのに、
# (1) WowHaus の 403 で毎回プログラムごと落ち、(2) 画像の取れない物件が
# スコア順で毎回先頭を埋めていた。予定が組めず 10/07 から投稿が止まる
# ところだった。

FORBIDDEN_URL = "https://example.com/forbidden/"


def _forbidden_http() -> FakeHttp:
    import httpx

    http = FakeHttp()
    real_get = http.get

    def get(url: str, **kwargs):
        if url == FORBIDDEN_URL:
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError(
                "Client error '403 Forbidden'", request=request,
                response=httpx.Response(403, request=request),
            )
        return real_get(url, **kwargs)

    http.get = get
    return http


def _set_score(conn, property_id: int, score: float) -> None:
    conn.execute("UPDATE properties SET score = ? WHERE id = ?", (score, property_id))
    conn.commit()


def test_掲載ページが開けなくても次の物件へ進む(config, conn) -> None:
    """**403 の1件で全体を止めない。** 以前は例外が素通りして終わっていた。"""
    blocked = _approved(conn, FORBIDDEN_URL)
    ok = _approved(conn)
    _set_score(conn, blocked, 99.0)   # 先に試される側を落ちる物件にする

    stats = deliver_approved(config, conn, drive=FakeDrive(), http=_forbidden_http())

    assert stats.failed == 1
    assert [d.property_id for d in stats.delivered] == [ok]


def test_失敗した物件は理由を残して後回しにする(config, conn) -> None:
    """審査UIの自動納品と同じ記録を残す。画面の「再試行」がそのまま効く。"""
    blocked = _approved(conn, FORBIDDEN_URL)
    deliver_approved(config, conn, drive=FakeDrive(), http=_forbidden_http())

    row = conn.execute(
        "SELECT status, delivery_attempts, delivery_attempted_at, delivery_error "
        "FROM properties WHERE id = ?", (blocked,),
    ).fetchone()
    assert row["status"] == "approved"   # 見送りにはしない。人の判断を変えない
    assert row["delivery_attempts"] == 1
    assert row["delivery_attempted_at"] is not None
    assert "403" in row["delivery_error"]


def test_失敗し続ける物件が後ろの順番を奪わない(config, conn) -> None:
    """**ここが4日間止まった本体。** 件数の枠を、毎回失敗する物件が埋めていた。"""
    blocked = _approved(conn, FORBIDDEN_URL)
    ok = _approved(conn)
    _set_score(conn, blocked, 99.0)
    http = _forbidden_http()

    first = deliver_approved(config, conn, limit=1, drive=FakeDrive(), http=http)
    assert first.failed == 1 and first.delivered == []
    # 2回目は、直近に失敗した物件を飛ばして後ろに順番が回る
    second = deliver_approved(config, conn, limit=1, drive=FakeDrive(), http=http)
    assert [d.property_id for d in second.delivered] == [ok]


def test_画像なしも記録する(config, conn) -> None:
    empty = _approved(conn, "https://example.com/empty/")
    http = FakeHttp()
    http.pages["https://example.com/empty/"] = _Response("<article></article>", "text/html")
    deliver_approved(config, conn, drive=FakeDrive(), http=http)

    row = conn.execute(
        "SELECT delivery_error FROM properties WHERE id = ?", (empty,)
    ).fetchone()
    assert row["delivery_error"].startswith("画像なし")


def test_待ち時間が過ぎれば試し直す(config, conn) -> None:
    """**一度の失敗で外さない。** 相手サイトの一時的な不調もある。"""
    from datetime import UTC, datetime, timedelta

    blocked = _approved(conn, FORBIDDEN_URL)
    old = datetime.now(UTC) - timedelta(seconds=config.delivery.retry_after_sec + 60)
    conn.execute(
        "UPDATE properties SET delivery_attempts = 1, delivery_attempted_at = ? "
        "WHERE id = ?", (old.isoformat(), blocked),
    )
    conn.commit()

    stats = deliver_approved(config, conn, drive=FakeDrive(), http=_forbidden_http())
    assert stats.failed == 1, "待ち時間が過ぎたのでもう一度試す"


def test_上限まで失敗したら自動では触らない(config, conn) -> None:
    """延々と相手サイトを叩かない。審査UIの「再試行」で人が戻す。"""
    blocked = _approved(conn, FORBIDDEN_URL)
    conn.execute(
        "UPDATE properties SET delivery_attempts = ? WHERE id = ?",
        (config.delivery.max_attempts, blocked),
    )
    conn.commit()

    stats = deliver_approved(config, conn, drive=FakeDrive(), http=_forbidden_http())
    assert stats.failed == 0 and stats.delivered == []


def test_Driveの失敗は物件のせいにしない(config, conn) -> None:
    """Drive が落ちているだけなら、復旧したらすぐ納品してほしい。"""
    property_id = _approved(conn)
    deliver_approved(config, conn, drive=FakeDrive(fail_on_upload=True), http=FakeHttp())
    row = conn.execute(
        "SELECT delivery_attempts FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    assert row["delivery_attempts"] == 0


def test_dry_runでは失敗を記録しない(config, conn) -> None:
    blocked = _approved(conn, FORBIDDEN_URL)
    deliver_approved(config, conn, dry_run=True, drive=FakeDrive(), http=_forbidden_http())
    row = conn.execute(
        "SELECT delivery_attempts FROM properties WHERE id = ?", (blocked,)
    ).fetchone()
    assert row["delivery_attempts"] == 0


# --- 記録だけ残ってファイルが無い画像を取り直す（2026-10-02） ----------
# Mac で画像を取ったあと納品まで行かなかった物件は、images の行だけが
# DB に残る。Actions にはそのファイルが無く、7件が止まっていた。

def _orphan_rows(conn, property_id: int, tmp_path) -> list[Path]:
    """Mac で取った体の行を作る（ファイルは置かない）。"""
    paths = []
    for position, name in enumerate(("a.jpg", "b.jpg"), start=1):
        path = tmp_path / "mac-only" / f"{position:02d}.jpg"
        conn.execute(
            "INSERT INTO images (property_id, source_url, width, height, local_path, "
            "position, fetched_at) VALUES (?, ?, 1600, 1200, ?, ?, '2026-08-01T00:00:00')",
            (property_id, f"https://example.com/photos/{name}", str(path), position),
        )
        paths.append(path)
    conn.commit()
    return paths


def test_手元に無い画像は元のURLから取り直して納品する(config, conn, tmp_path) -> None:
    property_id = _approved(conn)
    paths = _orphan_rows(conn, property_id, tmp_path)
    assert not any(p.exists() for p in paths)

    stats = deliver_approved(config, conn, drive=FakeDrive(), http=FakeHttp())

    assert [d.property_id for d in stats.delivered] == [property_id]
    assert stats.delivered[0].image_count == 2
    assert all(p.exists() for p in paths)


def test_取り直せない画像があっても残りで納品する(config, conn, tmp_path) -> None:
    property_id = _approved(conn)
    _orphan_rows(conn, property_id, tmp_path)
    http = FakeHttp()
    del http.pages["https://example.com/photos/b.jpg"]   # 1枚は取れない

    stats = deliver_approved(config, conn, drive=FakeDrive(), http=http)
    assert stats.delivered[0].image_count == 1


def test_robots_txtで禁止なら取り直さない(config, conn, tmp_path) -> None:
    """**収集方針は取り直しでも同じ。** 禁止されたら取らない。"""
    from freming.images.fetch import restore_missing_files
    from freming.net.client import RobotsDisallowed

    property_id = _approved(conn)
    paths = _orphan_rows(conn, property_id, tmp_path)

    class Blocked:
        def get(self, url, **_k):
            raise RobotsDisallowed(url)

    assert restore_missing_files(conn, property_id, Blocked()) == 0
    assert not any(p.exists() for p in paths)


# --- 写真を取るページを人が指定する（0024・2026-10-02） ------------------
# The Spaces は画像の配信元が robots.txt で禁止、WowHaus は記事が 403。
# どちらも迂回せず、人が貼った別のページから取る。

PAGE_URL = "https://agent.example.org/listing/123"


def _photo(width: int, height: int) -> bytes:
    """模様のある画像。**単色だと「写真なし」板として落とされる**（他サイトから
    取る分には is_flat_image がかかる）。"""
    img = Image.new("RGB", (width, height))
    img.putdata([((x * 7) % 256, (y * 5) % 256, (x + y) % 256)
                 for y in range(height) for x in range(width)])
    buffer = BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


def _with_page(http: FakeHttp) -> FakeHttp:
    http.pages[PAGE_URL] = _Response(
        '<main><img src="/p/one.jpg"><img src="/p/two.jpg"><img src="/p/three.jpg"></main>',
        "text/html",
    )
    for name in ("one", "two", "three"):
        http.pages[f"https://agent.example.org/p/{name}.jpg"] = _Response(
            _photo(800, 640), "image/jpeg"
        )
    return http


def _set_page(conn, property_id: int, url: str = PAGE_URL) -> None:
    conn.execute("UPDATE properties SET image_page_url = ? WHERE id = ?", (url, property_id))
    conn.commit()


def test_記事が403でも指定ページの写真で納品する(config, conn) -> None:
    """WowHaus の形。記事は開けないが、写真は別のページから揃う。"""
    property_id = _approved(conn, FORBIDDEN_URL)
    _set_page(conn, property_id)

    stats = deliver_approved(config, conn, drive=FakeDrive(),
                             http=_with_page(_forbidden_http()))

    assert [d.property_id for d in stats.delivered] == [property_id]
    assert stats.delivered[0].image_count == 3
    origins = {r["origin_url"] for r in conn.execute(
        "SELECT origin_url FROM images WHERE property_id = ?", (property_id,))}
    assert origins == {PAGE_URL}   # 取り消し（fill-images --undo）が効く印


def test_記事の写真が取れなくても指定ページの写真で納品する(config, conn) -> None:
    """The Spaces の形。記事は読めるが写真が1枚も取れない。"""
    property_id = _approved(conn, "https://example.com/empty/")
    _set_page(conn, property_id)
    http = _with_page(FakeHttp())
    http.pages["https://example.com/empty/"] = _Response("<article></article>", "text/html")

    stats = deliver_approved(config, conn, drive=FakeDrive(), http=http)
    assert stats.delivered[0].image_count == 3


def test_指定ページが無ければ今までどおり失敗する(config, conn) -> None:
    _approved(conn, FORBIDDEN_URL)
    stats = deliver_approved(config, conn, drive=FakeDrive(), http=_forbidden_http())
    assert stats.failed == 1 and stats.delivered == []


def test_自動収集禁止のサイトからは取らない(config, conn) -> None:
    """**人が貼っても、取りに行くのはこちらのプログラム。** Zillow などは不可。"""
    from freming.images.fetch import import_from_page

    property_id = _approved(conn)
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()

    class MustNotFetch:
        def get(self, url, **_k):
            raise AssertionError(f"取りに行ってはいけない: {url}")

    url = "https://www.zillow.com/homedetails/123"
    assert import_from_page(config, conn, row, url, MustNotFetch()) == 0


def test_robots_txtで禁止されたページからは取らない(config, conn) -> None:
    from freming.images.fetch import import_from_page
    from freming.net.client import RobotsDisallowed

    property_id = _approved(conn)
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()

    class Blocked:
        def get(self, url, **_k):
            raise RobotsDisallowed(url)

    assert import_from_page(config, conn, row, PAGE_URL, Blocked()) == 0
