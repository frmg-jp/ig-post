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
    ignored_prices: list[str] = field(default_factory=list)  # 文脈から売出価格でないと判断
    evidence: str = ""

    def is_candidate(self, threshold: int) -> bool:
        return self.score >= threshold


def _near_keyword(
    start: int, end: int, keyword_spans: list[tuple[int, int]], window: int
) -> bool:
    """価格表記が販売キーワードの近くにあるか。

    window が 0 のときは距離を問わない（すべて加点対象）。
    """
    if window <= 0:
        return True
    return any(
        start - window <= k_end and k_start <= end + window
        for k_start, k_end in keyword_spans
    )


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

    keyword_spans: list[tuple[int, int]] = []
    for keyword in config.keywords:
        lowered_keyword = keyword.lower()
        start = lowered.find(lowered_keyword)
        if start < 0:
            continue
        result.keywords.append(keyword)
        while start >= 0:
            keyword_spans.append((start, start + len(lowered_keyword)))
            start = lowered.find(lowered_keyword, start + 1)

    window = config.price_requires_keyword_within
    for pattern in config.price_patterns:
        try:
            matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
        except re.error:
            log.warning("price_patterns の正規表現が不正です: %s", pattern)
            continue
        for match in matches:
            found = match.group(0).strip()
            if not found:
                continue
            if _near_keyword(match.start(), match.end(), keyword_spans, window):
                if found not in result.prices:
                    result.prices.append(found)
            elif found not in result.ignored_prices:
                # 建設費・事業費など、売出価格ではない金額
                result.ignored_prices.append(found)

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
