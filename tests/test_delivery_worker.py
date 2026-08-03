"""承認から納品までの自動化のテスト。

自動で走るぶん、止まるべきところで止まることが最重要になる。

  - 失敗を無限に拾い直さない（上限に達したら自動では触らない）
  - 失敗しても承認済みのまま残り、審査UIから追跡・再試行できる
  - Drive の認証切れで対話（ブラウザ）に進まない
"""

from __future__ import annotations

import pytest

from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import (
    approve_property,
    delivery_queue,
    delivery_queue_size,
    record_delivery_failure,
    retry_delivery,
)
from freming.delivery.drive import DriveAuthError
from freming.delivery.worker import DeliveryWorker, describe_error
from tests.test_deliver import ARTICLE_URL, FakeDrive, FakeHttp, _approved


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "worker.db"
    cfg.images.work_dir = tmp_path / "images"
    cfg.delivery.max_attempts = 2
    cfg.delivery.retry_after_sec = 0
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


def _worker(config, drive=None, http=None) -> DeliveryWorker:
    """本物のクライアントを作らせないよう、先に差し替えたワーカーを返す。"""
    worker = DeliveryWorker(config)
    worker._drive = drive if drive is not None else FakeDrive()
    worker._http = http if http is not None else FakeHttp()
    return worker


# ----------------------------------------------------------------------
# キューの選び方
# ----------------------------------------------------------------------
def test_queue_contains_newly_approved(config, conn) -> None:
    _approved(conn)
    rows = delivery_queue(conn, limit=5, max_attempts=3, retry_after_sec=600)
    assert len(rows) == 1


def test_queue_drops_property_at_attempt_limit(config, conn) -> None:
    """上限まで失敗したら自動では拾わない。取れない画像を取りに行き続けない。"""
    property_id = _approved(conn)
    assert record_delivery_failure(conn, property_id, "画像なし") == 1
    assert delivery_queue(conn, limit=5, max_attempts=2, retry_after_sec=0)

    assert record_delivery_failure(conn, property_id, "画像なし") == 2
    assert delivery_queue(conn, limit=5, max_attempts=2, retry_after_sec=0) == []


def test_queue_waits_before_retrying_a_failure(config, conn) -> None:
    property_id = _approved(conn)
    record_delivery_failure(conn, property_id, "一時的なエラー")
    assert delivery_queue(conn, limit=5, max_attempts=3, retry_after_sec=600) == []


def test_untried_is_picked_before_failed(config, conn) -> None:
    """失敗続きの候補の後ろで、新しい承認が待たされないこと。"""
    failed_id = _approved(conn, url=ARTICLE_URL)
    record_delivery_failure(conn, failed_id, "一時的なエラー")
    fresh_id = _approved(conn, url="https://example.com/another/")

    rows = delivery_queue(conn, limit=5, max_attempts=3, retry_after_sec=0)
    assert [row["id"] for row in rows] == [fresh_id, failed_id]


def test_retry_from_ui_restores_the_property(config, conn) -> None:
    property_id = _approved(conn)
    record_delivery_failure(conn, property_id, "画像なし")
    record_delivery_failure(conn, property_id, "画像なし")
    assert delivery_queue(conn, limit=5, max_attempts=2, retry_after_sec=0) == []

    assert retry_delivery(conn, property_id) is True
    rows = delivery_queue(conn, limit=5, max_attempts=2, retry_after_sec=0)
    assert [row["id"] for row in rows] == [property_id]


def test_reapproving_clears_the_failure_record(config, conn) -> None:
    """再承認しても上限に達したままだと、何も起きないように見えてしまう。"""
    property_id = _approved(conn)
    record_delivery_failure(conn, property_id, "画像なし")
    record_delivery_failure(conn, property_id, "画像なし")

    approve_property(conn, property_id)

    row = conn.execute(
        "SELECT delivery_attempts, delivery_error FROM properties WHERE id = ?",
        (property_id,),
    ).fetchone()
    assert row["delivery_attempts"] == 0
    assert row["delivery_error"] is None


def test_queue_size_excludes_given_up_properties(config, conn) -> None:
    property_id = _approved(conn)
    assert delivery_queue_size(conn, max_attempts=2) == 1
    record_delivery_failure(conn, property_id, "画像なし")
    record_delivery_failure(conn, property_id, "画像なし")
    assert delivery_queue_size(conn, max_attempts=2) == 0


# ----------------------------------------------------------------------
# ワーカーの動き
# ----------------------------------------------------------------------
def test_worker_delivers_approved_property(config, conn) -> None:
    property_id = _approved(conn)
    drive = FakeDrive()
    worker = _worker(config, drive=drive)

    delivered = worker.drain_once()

    assert [d.folder_name for d in delivered] == ["frmg_ig001"]
    assert [name for _, name in drive.uploads] == ["01.jpg", "02.jpg", "meta.txt"]
    assert conn.execute(
        "SELECT status FROM properties WHERE id = ?", (property_id,)
    ).fetchone()["status"] == "delivered"


def test_worker_does_not_deliver_twice(config, conn) -> None:
    """自動でも「再実行しても重複納品しない」ことは変わらない。"""
    _approved(conn)
    drive = FakeDrive()
    worker = _worker(config, drive=drive)

    worker.drain_once()
    worker.drain_once()

    assert len(drive.folders) == 1


def test_worker_records_failure_and_gives_up(config, conn) -> None:
    property_id = _approved(conn)
    worker = _worker(config, drive=FakeDrive(fail_on_upload=True))

    for _ in range(config.delivery.max_attempts):
        assert worker.drain_once() == []

    row = conn.execute(
        "SELECT status, delivery_attempts, delivery_error FROM properties WHERE id = ?",
        (property_id,),
    ).fetchone()
    # 承認のまま残す。失敗を別ステータスにすると一覧から消えて追跡できない
    assert row["status"] == "approved"
    assert row["delivery_attempts"] == config.delivery.max_attempts
    assert "アップロードに失敗" in row["delivery_error"]

    # 上限に達したので、以降の巡回では手を出さない
    worker.drain_once()
    assert conn.execute(
        "SELECT delivery_attempts FROM properties WHERE id = ?", (property_id,)
    ).fetchone()["delivery_attempts"] == config.delivery.max_attempts


def test_one_failure_does_not_stop_the_batch(config, conn) -> None:
    """1件の失敗で巡回を止めない。後ろの候補が巻き添えで止まらないこと。"""
    broken_id = _approved(conn, url="https://example.com/missing/")
    good_id = _approved(conn, url=ARTICLE_URL)
    # 先に失敗させて、good より前に並ぶようにする
    conn.execute("UPDATE properties SET score = 99 WHERE id = ?", (broken_id,))
    conn.commit()

    worker = _worker(config)
    delivered = worker.drain_once()

    assert [d.property_id for d in delivered] == [good_id]
    assert conn.execute(
        "SELECT delivery_error FROM properties WHERE id = ?", (broken_id,)
    ).fetchone()["delivery_error"]


def test_auth_error_stops_the_round_without_consuming_attempts(config, conn) -> None:
    """認証切れは物件の問題ではない。試行回数を使い切らせない。"""
    property_id = _approved(conn)
    worker = DeliveryWorker(config)
    worker._http = FakeHttp()

    def _fail() -> None:
        raise DriveAuthError("Drive の認証が切れています")

    worker._ensure_drive = _fail  # type: ignore[method-assign]
    assert worker.drain_once() == []

    assert conn.execute(
        "SELECT delivery_attempts FROM properties WHERE id = ?", (property_id,)
    ).fetchone()["delivery_attempts"] == 0


def test_describe_error_keeps_it_to_one_line(config) -> None:
    text = describe_error(DriveAuthError("認証が切れています\n詳細はログを見てください"))
    assert text == "DriveAuthError: 認証が切れています"


# ----------------------------------------------------------------------
# 審査UIと繋いだ通し確認
# ----------------------------------------------------------------------
def test_approving_in_the_ui_delivers_without_any_cli(config, conn) -> None:
    """承認 → 画像取得 → 加工 → 納品 が、別ターミナルなしで通ること。"""
    import time

    from fastapi.testclient import TestClient

    from freming.web.app import create_app

    drive = FakeDrive()
    worker = _worker(config, drive=drive)
    property_id = _approved(conn)
    conn.execute("UPDATE properties SET status = 'pending' WHERE id = ?", (property_id,))
    conn.commit()

    # with で入るとワーカーのスレッドが起動する（serve と同じ経路）
    with TestClient(create_app(config, worker=worker)) as client:
        client.post(f"/p/{property_id}/approve", data={"status": "pending"})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if drive.folders:
                break
            time.sleep(0.05)

    assert list(drive.folders.values()) == ["frmg_ig001"]
    assert conn.execute(
        "SELECT status FROM properties WHERE id = ?", (property_id,)
    ).fetchone()["status"] == "delivered"
