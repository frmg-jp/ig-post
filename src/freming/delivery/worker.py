"""承認から納品までの自動化。

    審査UIで承認 → ワーカーが拾う → 画像取得 → 加工 → Drive納品

審査UI（serve）の中でバックグラウンドスレッドとして動く。承認のたびに
起こされ、承認済みで未納品のものを順に納品する。別ターミナルで
deliver を実行する必要はない。

守っていること:

  - 納品は1件ずつ直列。画像取得は相手サイトへのアクセスなので、
    [1] 収集と同じ「間隔3秒以上・同一ドメインへの並列アクセス禁止」を
    そのまま適用する（HttpClient が強制する）。
  - 失敗は delivery_attempts に数え、上限に達したら自動では触らない。
    取れない画像を延々と取りに行かないため。復帰は審査UIの再試行から。
  - Drive の対話的な再認証には進まない。人が画面の前にいるとは限らない
    経路なので、認証が切れていればエラーとして記録し、check-drive に案内する。
"""

from __future__ import annotations

import threading

from freming.config import Config
from freming.db.connection import DbConnection, Row, connect
from freming.db.repository import delivery_queue, record_delivery_failure
from freming.delivery.deliver import DeliveryResult, deliver_property
from freming.delivery.drive import DriveAuthError, DriveClient, build_client
from freming.delivery.lock import DeliveryInProgress, delivery_lock
from freming.logging_setup import get_logger
from freming.net.client import HttpClient

log = get_logger(__name__)


def describe_error(exc: BaseException) -> str:
    """審査UIに出す1行の失敗理由。例外の型名だけでは何が起きたか分からない。"""
    text = str(exc).strip().splitlines()
    head = text[0] if text else ""
    return f"{type(exc).__name__}: {head}" if head else type(exc).__name__


class DeliveryWorker:
    """承認済みを順に納品するワーカー。

    スレッドは1本だけ。SQLite の接続はスレッドをまたげないので、
    ワーカー側で自分の接続を持つ（審査UIのリクエストとは別接続）。
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._wakeup = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._drive: DriveClient | None = None
        self._http: HttpClient | None = None
        # 審査UIに「いま何を納品中か」を出すための目印。読むだけなのでロックは持たない。
        self.current_property_id: int | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name="freming-delivery", daemon=True
        )
        self._thread.start()
        log.info(
            "自動納品を開始しました（巡回間隔 %.0f 秒 / 1巡 %d 件まで）",
            self.config.delivery.poll_interval_sec,
            self.config.delivery.batch_limit,
        )

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        self._wakeup.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._close_clients()
        log.info("自動納品を停止しました")

    def wake(self) -> None:
        """承認直後に呼ぶ。巡回間隔を待たずに納品を始める。"""
        self._wakeup.set()

    # ------------------------------------------------------------------
    # 本体
    # ------------------------------------------------------------------
    def _run(self) -> None:
        # 巡回の間ずっとロックを持つ。launchd の定期実行と serve のワーカーが
        # 同時に納品すると、同じ frmg_igNNN を取り合って二重納品になる。
        # serve が開いている間は、定期実行の側が退く。
        try:
            with delivery_lock(self.config):
                self._loop()
        except DeliveryInProgress as exc:
            self.last_error = str(exc)
            log.warning(
                "%s\n"
                "  この審査UIからは納品しません。定期実行の側が納品します。",
                exc,
            )

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.drain_once()
            except Exception:  # ワーカーは何があっても止めない
                log.exception("自動納品の巡回でエラーが発生しました")
            self._wakeup.wait(self.config.delivery.poll_interval_sec)
            self._wakeup.clear()

    def drain_once(self) -> list[DeliveryResult]:
        """いま納品できるものを納品する。テストから直接呼べるように分けてある。"""
        delivered: list[DeliveryResult] = []
        conn = connect(self.config.app.target())
        try:
            rows = delivery_queue(
                conn,
                limit=self.config.delivery.batch_limit,
                max_attempts=self.config.delivery.max_attempts,
                retry_after_sec=self.config.delivery.retry_after_sec,
            )
            if not rows:
                return delivered
            log.info("自動納品: 対象 %d 件", len(rows))
            for row in rows:
                if self._stopping.is_set():
                    break
                try:
                    result = self._deliver_one(conn, row)
                except DriveAuthError:
                    # 認証が切れている間は何件試しても同じなので、この巡回は打ち切る。
                    break
                if result is not None:
                    delivered.append(result)
        finally:
            conn.close()
            self.current_property_id = None
        return delivered

    def _deliver_one(
        self, conn: DbConnection, row: Row
    ) -> DeliveryResult | None:
        property_id = int(row["id"])
        self.current_property_id = property_id
        try:
            drive = self._ensure_drive()
            http = self._ensure_http()
            result = deliver_property(self.config, conn, row, drive, http)
        except DriveAuthError as exc:
            # 認証切れは物件ごとの問題ではない。試行回数を減らさず、
            # 巡回ごとにログへ出して check-drive に誘導する。
            self.last_error = describe_error(exc)
            log.error("自動納品を中断します（Drive認証）: %s", exc)
            raise
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で巡回を止めない
            attempts = record_delivery_failure(conn, property_id, describe_error(exc))
            self.last_error = describe_error(exc)
            limit = self.config.delivery.max_attempts
            if attempts >= limit:
                log.error(
                    "自動納品に%d回失敗したため諦めます: property_id=%s (%s)"
                    "／審査UIの「再試行」で再開できます",
                    attempts, property_id, exc,
                )
            else:
                log.warning(
                    "自動納品に失敗しました（%d/%d回目）: property_id=%s (%s)",
                    attempts, limit, property_id, exc,
                )
            return None
        finally:
            self.current_property_id = None

        if result is None:
            # すでに納品済み。status だけ揃えておく（deliveries が正）。
            conn.execute(
                "UPDATE properties SET status = 'delivered' WHERE id = ?", (property_id,)
            )
            conn.commit()
            log.info("納品済みだったので status を揃えました: property_id=%s", property_id)
            return None

        self.last_error = None
        log.info("自動納品しました: %s（%d枚）", result.folder_name, result.image_count)
        return result

    # ------------------------------------------------------------------
    # クライアント。使うときに作り、以降は使い回す
    # ------------------------------------------------------------------
    def _ensure_drive(self) -> DriveClient:
        if self._drive is None:
            # allow_interactive=False: 同意画面を開いて待ち続けないようにする
            self._drive = build_client(self.config.drive, allow_interactive=False)
        return self._drive

    def _ensure_http(self) -> HttpClient:
        if self._http is None:
            self._http = HttpClient(self.config.http)
        return self._http

    def _close_clients(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
        self._drive = None
