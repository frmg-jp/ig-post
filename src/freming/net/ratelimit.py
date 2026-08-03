"""ドメイン単位のレート制限。

同一ドメインへは「最低間隔をあけて」「同時に1本だけ」アクセスする。
ドメインごとのロックを保持したままリクエストを行うことで、間隔の遵守と
並列禁止を同じ仕組みで担保する。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlparse

from freming.logging_setup import get_logger

log = get_logger(__name__)


def domain_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


class DomainRateLimiter:
    """ドメインごとに最低間隔をあけ、同時実行を1本に制限する。"""

    def __init__(self, min_interval_sec: float, sleep: callable = time.sleep) -> None:
        self.min_interval_sec = min_interval_sec
        self._sleep = sleep
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._last_request_at: dict[str, float] = {}

    def _lock_for(self, domain: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(domain, threading.Lock())

    def interval_for(self, domain: str) -> float:
        """そのドメインに適用する間隔（robots.txt の Crawl-delay で上書きされうる）。"""
        return self.min_interval_sec

    @contextmanager
    def hold(self, url: str, min_interval_sec: float | None = None) -> Iterator[None]:
        """対象ドメインのロックを取り、必要な時間だけ待ってから処理を通す。"""
        domain = domain_of(url)
        interval = self.min_interval_sec if min_interval_sec is None else min_interval_sec
        lock = self._lock_for(domain)
        lock.acquire()
        try:
            last = self._last_request_at.get(domain)
            if last is not None:
                wait = interval - (time.monotonic() - last)
                if wait > 0:
                    log.debug("%s: レート制限のため %.2f 秒待機", domain, wait)
                    self._sleep(wait)
            yield
        finally:
            self._last_request_at[domain] = time.monotonic()
            lock.release()
