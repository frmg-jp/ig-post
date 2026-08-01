"""robots.txt の取得と判定。

方針:
- ドメインごとに1回だけ取得してキャッシュする。
- 取得できなかった場合（ネットワークエラー等）は「許可されていない」と扱う。
  安全側に倒す。404 の場合のみ、robots.txt が存在しない＝制限なしとして許可する。
- Crawl-delay が設定間隔より長ければ、そちらに従う。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from freming.logging_setup import get_logger
from freming.net.ratelimit import domain_of

log = get_logger(__name__)


@dataclass
class RobotsRules:
    parser: RobotFileParser | None
    fetched: bool
    missing: bool = False   # robots.txt が 404（＝制限なし）

    def allows(self, url: str, user_agent: str) -> bool:
        if self.missing:
            return True
        if not self.fetched or self.parser is None:
            return False
        return self.parser.can_fetch(user_agent, url)

    def crawl_delay(self, user_agent: str) -> float | None:
        if self.parser is None:
            return None
        try:
            delay = self.parser.crawl_delay(user_agent)
        except Exception:  # noqa: BLE001 - 実装差異に備える
            return None
        return float(delay) if delay is not None else None


class RobotsPolicy:
    """robots.txt のキャッシュと判定。"""

    def __init__(self, user_agent: str, timeout_sec: float = 15.0, enabled: bool = True) -> None:
        self.user_agent = user_agent
        self.timeout_sec = timeout_sec
        self.enabled = enabled
        self._cache: dict[str, RobotsRules] = {}

    def _fetch(self, robots_url: str) -> RobotsRules:
        try:
            response = httpx.get(
                robots_url,
                timeout=self.timeout_sec,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            log.warning("robots.txt を取得できませんでした（%s）: %s", robots_url, exc)
            return RobotsRules(parser=None, fetched=False)

        if response.status_code == 404:
            log.info("robots.txt がありません（制限なしとして扱う）: %s", robots_url)
            return RobotsRules(parser=None, fetched=True, missing=True)
        if response.status_code >= 400:
            log.warning("robots.txt の取得に失敗 (status=%s): %s", response.status_code, robots_url)
            return RobotsRules(parser=None, fetched=False)

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        log.info("robots.txt を読み込みました: %s", robots_url)
        return RobotsRules(parser=parser, fetched=True)

    def rules_for(self, url: str) -> RobotsRules:
        domain = domain_of(url)
        if domain not in self._cache:
            parsed = urlparse(url)
            robots_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/robots.txt")
            self._cache[domain] = self._fetch(robots_url)
        return self._cache[domain]

    def is_allowed(self, url: str) -> bool:
        if not self.enabled:  # pragma: no cover - 設定で false にはできない
            return True
        return self.rules_for(url).allows(url, self.user_agent)

    def crawl_delay(self, url: str) -> float | None:
        return self.rules_for(url).crawl_delay(self.user_agent)
