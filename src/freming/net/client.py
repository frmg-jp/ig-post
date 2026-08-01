"""HTTPクライアント。

すべての外部アクセスはここを通す。robots.txt の判定、ドメイン単位の
レート制限、リトライを一箇所に集約し、収集モジュール側で個別に
気をつけなくてもポリシーが守られるようにする。
"""

from __future__ import annotations

import random
import time

import httpx

from freming.config import HttpConfig
from freming.logging_setup import get_logger
from freming.net.ratelimit import DomainRateLimiter, domain_of
from freming.net.robots import RobotsPolicy

log = get_logger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RobotsDisallowed(RuntimeError):
    """robots.txt で許可されていないURL。"""

    def __init__(self, url: str) -> None:
        super().__init__(
            f"robots.txt により許可されていないため取得しません: {url}\n"
            "  このサイトは自動取得が許可されていません。回避は行いません。"
        )
        self.url = url


class HttpClient:
    """robots.txt とレート制限を強制するHTTPクライアント。"""

    def __init__(self, config: HttpConfig) -> None:
        self.config = config
        self.robots = RobotsPolicy(
            user_agent=config.user_agent,
            timeout_sec=config.timeout_sec,
            enabled=config.respect_robots_txt,
        )
        self.limiter = DomainRateLimiter(config.request_interval_sec)
        self._client = httpx.Client(
            headers={"User-Agent": config.user_agent},
            timeout=config.timeout_sec,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    def get(self, url: str, *, allow_status: tuple[int, ...] = ()) -> httpx.Response:
        """robots.txt を確認し、間隔をあけて GET する。

        Raises:
            RobotsDisallowed: robots.txt が許可していない場合。
            httpx.HTTPStatusError: リトライしても解消しないHTTPエラー。
        """
        if not self.robots.is_allowed(url):
            log.warning("robots.txt により取得をスキップ: %s", url)
            raise RobotsDisallowed(url)

        interval = self._interval_for(url)
        attempts = self.config.max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            with self.limiter.hold(url, min_interval_sec=interval):
                try:
                    log.debug("GET %s (%d/%d)", url, attempt, attempts)
                    response = self._client.get(url)
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt >= attempts:
                        log.error("取得に失敗しました: %s (%s)", url, exc)
                        raise
                    self._backoff(attempt, url, str(exc))
                    continue

            if response.status_code in allow_status or response.status_code < 400:
                return response

            if response.status_code in _RETRYABLE_STATUS and attempt < attempts:
                retry_after = _retry_after_seconds(response)
                self._backoff(attempt, url, f"status={response.status_code}", retry_after)
                continue

            log.error("取得に失敗しました: %s (status=%s)", url, response.status_code)
            response.raise_for_status()

        if last_exc:  # pragma: no cover - 上のraiseで抜けるはず
            raise last_exc
        raise RuntimeError(f"取得に失敗しました: {url}")

    # ------------------------------------------------------------------
    def _interval_for(self, url: str) -> float:
        """robots.txt の Crawl-delay が設定より長ければそちらに従う。"""
        configured = self.config.request_interval_sec
        delay = self.robots.crawl_delay(url)
        if delay is not None and delay > configured:
            log.info("%s: robots.txt の Crawl-delay %.1f 秒に従います", domain_of(url), delay)
            return delay
        return configured

    def _backoff(self, attempt: int, url: str, reason: str, retry_after: float | None = None) -> None:
        wait = retry_after if retry_after is not None else (
            self.config.backoff_factor ** (attempt - 1) + random.uniform(0, 0.5)
        )
        log.warning("再試行します: %s (%s) — %.1f秒待機", url, reason, wait)
        time.sleep(wait)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Retry-After ヘッダがあれば尊重する。"""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
