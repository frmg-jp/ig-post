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
