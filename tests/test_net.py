"""アクセスポリシー（robots.txt・レート制限）の検証。"""

from __future__ import annotations

from urllib.robotparser import RobotFileParser

import pytest

from freming.net.ratelimit import DomainRateLimiter, domain_of
from freming.net.robots import RobotsPolicy, RobotsRules

UA = "FremingCuratedBot/0.1 (+https://freming.jp/bot; contact@freming.jp)"

ROBOTS = """
User-agent: *
Crawl-delay: 10
Disallow: /private/
Disallow: /search

User-agent: BadBot
Disallow: /
"""


def _rules(body: str) -> RobotsRules:
    parser = RobotFileParser()
    parser.parse(body.splitlines())
    return RobotsRules(parser=parser, fetched=True)


def test_disallowed_path_is_blocked() -> None:
    rules = _rules(ROBOTS)
    assert rules.allows("https://example.com/articles/1", UA)
    assert not rules.allows("https://example.com/private/x", UA)
    assert not rules.allows("https://example.com/search?q=loft", UA)


def test_crawl_delay_is_read() -> None:
    assert _rules(ROBOTS).crawl_delay(UA) == 10.0


def test_unfetchable_robots_blocks_everything() -> None:
    """取得できなかったときは安全側に倒して許可しない。"""
    rules = RobotsRules(parser=None, fetched=False)
    assert not rules.allows("https://example.com/anything", UA)


def test_missing_robots_allows_everything() -> None:
    """404 は robots.txt が無い＝制限なし。"""
    rules = RobotsRules(parser=None, fetched=True, missing=True)
    assert rules.allows("https://example.com/anything", UA)


def test_policy_caches_per_domain(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch(self, robots_url: str) -> RobotsRules:
        calls.append(robots_url)
        return _rules(ROBOTS)

    monkeypatch.setattr(RobotsPolicy, "_fetch", fake_fetch)
    policy = RobotsPolicy(user_agent=UA)

    assert policy.is_allowed("https://example.com/a")
    assert policy.is_allowed("https://example.com/b")
    assert not policy.is_allowed("https://example.com/private/c")
    assert calls == ["https://example.com/robots.txt"]  # 1ドメイン1回だけ


def test_rate_limiter_waits_between_requests_to_same_domain() -> None:
    slept: list[float] = []
    limiter = DomainRateLimiter(min_interval_sec=3.0, sleep=slept.append)

    with limiter.hold("https://example.com/a"):
        pass
    with limiter.hold("https://example.com/b"):
        pass

    assert len(slept) == 1
    assert slept[0] == pytest.approx(3.0, abs=0.2)


def test_rate_limiter_does_not_wait_across_domains() -> None:
    slept: list[float] = []
    limiter = DomainRateLimiter(min_interval_sec=3.0, sleep=slept.append)

    with limiter.hold("https://a.example.com/x"):
        pass
    with limiter.hold("https://b.example.com/y"):
        pass

    assert slept == []


def test_domain_of() -> None:
    assert domain_of("https://WWW.Example.com/path") == "www.example.com"
