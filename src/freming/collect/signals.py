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
    # 「なぜ点が付かなかったか」を人が読める形にするための抜粋。
    # キーワードは出たが価格が出ない（またはその逆）とき、本文のどこを
    # 見て判定したのかが分からないと、キーワード表現の不足なのか価格の
    # 書式なのかを切り分けられず、推測で設定をいじることになる。
    keyword_context: str = ""
    price_context: str = ""

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


def _context(text: str, start: int, end: int, width: int = 70) -> str:
    """該当箇所の前後を切り出す。取りこぼしの原因を目で確かめるため。"""
    head = max(0, start - width)
    tail = min(len(text), end + width)
    snippet = " ".join(text[head:tail].split())
    return f"…{snippet}…" if head or tail < len(text) else snippet


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
        if not result.keyword_context:
            result.keyword_context = _context(text, start, start + len(keyword))
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
            if not result.price_context:
                result.price_context = _context(text, match.start(), match.end())
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


# 販売サイトのドメインではあるが、物件のページではないもの。仲介業者の
# 自己紹介や検索トップに当たると、ストーリーズに貼っても物件が出てこない。
#
# 実例（2026-08-01）: CIRCA の記事にあった Compass のエージェント紹介ページ
# （/agents/…）を「売出中の裏付け」として拾っていた。
_NOT_A_LISTING = re.compile(
    r"/(?:agents?|team|about|contact|offices?|careers?|blog|search|"
    r"privacy|terms|login|signup)(?:/|$)",
    re.IGNORECASE,
)


def pick_listing_url(links: list[str]) -> str | None:
    """販売サイトへのリンクから、**物件のページ**を1つ選ぶ。

    記事末尾の「この物件は Compass に掲載中」のリンクを、ストーリーズに
    そのまま貼れる形で取り出すためのもの。選び方:

      - 業者紹介・検索トップなどは外す（物件が出てこないため）
      - 物件ページのURLには番地や掲載IDが入る（`/homedetails/521-ne-6th-st…
        /43…_zpid/`、`/listing/…/1234567890`）ので、**数字を含むものを優先**
      - どれも当たらなければ、いちばん深い階層のものを使う（トップページより
        物件ページの方が深い）

    候補が無ければ None。
    """
    usable = []
    for link in links:
        path = urlparse(link).path or "/"
        if path in ("", "/") or _NOT_A_LISTING.search(path):
            continue
        usable.append((link, path))
    if not usable:
        return None
    with_digits = [item for item in usable if any(c.isdigit() for c in item[1])]
    pool = with_digits or usable
    return max(pool, key=lambda item: item[1].count("/"))[0]


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
