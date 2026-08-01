"""販売シグナルの検出。

編集ソースの記事の大半は「販売中の物件」ではなく単なる建築紹介なので、
本文と外部リンクから「今売りに出ている」ことを示す手がかりを探し、
一定点数に達したものだけを候補化する。

ここでの判定はあくまで足切り。最終的な販売可否の判断は [2] スコアリングで
LLM が行う。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from freming.config import ForSaleSignals
from freming.logging_setup import get_logger

log = get_logger(__name__)

_SNIPPET_RADIUS = 60


@dataclass
class SignalResult:
    score: int = 0
    keywords: list[str] = field(default_factory=list)
    prices: list[str] = field(default_factory=list)
    listing_links: list[str] = field(default_factory=list)
    evidence: str = ""

    def is_candidate(self, threshold: int) -> bool:
        return self.score >= threshold


def _snippet(text: str, needle: str) -> str:
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return ""
    start = max(0, idx - _SNIPPET_RADIUS)
    end = min(len(text), idx + len(needle) + _SNIPPET_RADIUS)
    return " ".join(text[start:end].split())


def detect(text: str, links: list[str], config: ForSaleSignals) -> SignalResult:
    """本文とリンクから販売シグナルを検出する。"""
    result = SignalResult()
    lowered = text.lower()

    for keyword in config.keywords:
        if keyword.lower() in lowered:
            result.keywords.append(keyword)

    for pattern in config.price_patterns:
        try:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                found = match.group(0).strip()
                if found and found not in result.prices:
                    result.prices.append(found)
        except re.error:
            log.warning("price_patterns の正規表現が不正です: %s", pattern)

    listing_domains = [d.lower() for d in config.listing_domains]
    for link in links:
        netloc = (urlparse(link).netloc or "").lower()
        if not netloc:
            continue
        if any(netloc == d or netloc.endswith("." + d) for d in listing_domains):
            if link not in result.listing_links:
                result.listing_links.append(link)

    if result.keywords:
        result.score += config.keyword_score
    if result.prices:
        result.score += config.price_score
    if result.listing_links:
        result.score += config.listing_link_score

    result.evidence = _build_evidence(text, result)
    return result


def _build_evidence(text: str, result: SignalResult) -> str:
    """人が読んで納得できる根拠テキストを組み立てる。"""
    parts: list[str] = []
    if result.keywords:
        snippet = _snippet(text, result.keywords[0])
        parts.append(f'記載: "{snippet}"' if snippet else f"キーワード: {result.keywords[0]}")
    if result.prices:
        parts.append(f"価格表記: {', '.join(result.prices[:3])}")
    if result.listing_links:
        hosts = []
        for link in result.listing_links[:3]:
            host = urlparse(link).netloc
            if host not in hosts:
                hosts.append(host)
        parts.append(f"不動産サイトへのリンク: {', '.join(hosts)}")
    return " / ".join(parts)
