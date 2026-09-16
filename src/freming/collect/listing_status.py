"""[1] 販売状況（Listing Status）を掲載ページで確かめる。

    販売ページを開く → 状態を読む → 根拠ごと残す

**記事があるだけでは「販売中」と言わない。** いまの is_for_sale は記事に
売出の signal があるかの判定で、書かれた時点の話でしかない。半年前の記事が
残っていれば、売れていても販売中に見える。

読み取り方は、確からしい順に3つ:

  1. **構造化データ**（JSON-LD の `availability`）。サイトが自分で機械向けに
     書いている値なので、いちばん信用できる
  2. **状態のラベル**（`Status: Sold` のような並び）
  3. **決まり文句**（`no longer available` / `under contract` …）

どれにも当たらなければ `unknown` にする。**推測しない。**「見たが
読み取れなかった」と「見ていない」も区別する（後者は NULL のまま）。

自動収集が禁止されているサイト（Zillow / Redfin / Compass など）は
**開かない**。そこにしか無い物件は確認できないまま残る——編集方針も
「確認できない場合は Current availability unconfirmed と明記する」と
言っていて、そちらに合わせる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from freming.config import Config
from freming.db.connection import DbConnection, Row
from freming.logging_setup import get_logger
from freming.net.client import HttpClient, RobotsDisallowed

log = get_logger(__name__)

ACTIVE = "active"
PENDING = "pending"
SOLD = "sold"
OFF_MARKET = "off_market"
UNKNOWN = "unknown"

LABELS = {
    ACTIVE: "販売中",
    PENDING: "商談中",
    SOLD: "成約済み",
    OFF_MARKET: "取り下げ",
    UNKNOWN: "読み取れず",
}


@dataclass
class StatusResult:
    """1件の確認結果。**根拠を必ず持つ。**"""

    value: str
    evidence: str
    url: str | None = None
    mls: str | None = None

    @property
    def label(self) -> str:
        return LABELS.get(self.value, self.value)


# 1. 構造化データ。schema.org の Offer.availability。
_AVAILABILITY = re.compile(
    r'"availability"\s*:\s*"(?:https?://schema\.org/)?([A-Za-z]+)"', re.I
)
_AVAILABILITY_MAP = {
    "instock": ACTIVE,
    "onlineonly": ACTIVE,
    "limitedavailability": ACTIVE,
    "preorder": PENDING,
    "backorder": PENDING,
    "soldout": SOLD,
    "outofstock": OFF_MARKET,
    "discontinued": OFF_MARKET,
}

# 2. 状態のラベル。`Status: Sold` / `listingStatus":"Active"` など。
_LABEL = re.compile(
    r"(?:listing[_\s-]*)?status\W{0,12}?"
    r"(active|for\s+sale|pending|contingent|under\s+contract|sold|closed|"
    r"withdrawn|expired|off[\s-]?market|coming\s+soon)\b",
    re.I,
)
_LABEL_MAP = {
    "active": ACTIVE, "for sale": ACTIVE, "coming soon": PENDING,
    "pending": PENDING, "contingent": PENDING, "under contract": PENDING,
    "sold": SOLD, "closed": SOLD,
    "withdrawn": OFF_MARKET, "expired": OFF_MARKET, "off market": OFF_MARKET,
    "off-market": OFF_MARKET,
}

# 3. 決まり文句。**単なる "sold" は採らない**（沿革の「1998年に売却」を
#    拾ってしまう）。状態を名指ししている言い回しだけを見る。
_PHRASES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"no longer (?:available|on the market|for sale)", re.I), OFF_MARKET),
    (re.compile(r"this (?:listing|property) (?:has been|was) removed", re.I), OFF_MARKET),
    (re.compile(r"listing (?:has )?expired", re.I), OFF_MARKET),
    (re.compile(r"(?:sale|sold)\s+pending", re.I), PENDING),
    (re.compile(r"under\s+contract", re.I), PENDING),
    (re.compile(r"(?:just|recently)\s+sold", re.I), SOLD),
    (re.compile(r"\bsold\s+(?:on|for)\b", re.I), SOLD),
]

_MLS = re.compile(
    r"MLS\s*(?:®|\(r\))?\s*(?:#|no\.?|number|id)?\s*[:#]?\s*([A-Z]{0,3}[0-9]{5,12}[A-Z]?)",
    re.I,
)


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def blocked_domains(config: Config) -> set[str]:
    """こちらから開かないサイト。

    **自動収集を禁じているサイトの一覧を1か所に持たない。** 手動URL投入
    だけにしてある販売ソース（mode: manual_only）と、画像検索の結果から
    外しているドメインを、そのまま両方使う。片方にだけ足されて、もう
    片方から漏れるのを避ける。
    """
    out = {d.lower() for d in config.images.discovery.blocked_domains}
    for source in config.listing_sources:
        if source.mode == "manual_only" and source.base_url:
            host = _domain(source.base_url)
            if host:
                out.add(host)
    return out


def can_check(config: Config, url: str | None) -> bool:
    if not url or not url.startswith("http"):
        return False
    host = _domain(url)
    return not any(
        host == blocked or host.endswith("." + blocked)
        for blocked in blocked_domains(config)
    )


def detect(html: str) -> StatusResult:
    """ページの中身から販売状況を読む。**推測しない。**"""
    found = _AVAILABILITY.search(html)
    if found:
        value = _AVAILABILITY_MAP.get(found.group(1).lower())
        if value:
            return StatusResult(value, f"構造化データ: {found.group(0)[:80]}")

    found = _LABEL.search(html)
    if found:
        word = re.sub(r"\s+", " ", found.group(1)).lower()
        value = _LABEL_MAP.get(word)
        if value:
            return StatusResult(value, f"表示: {found.group(0)[:80]}")

    for pattern, value in _PHRASES:
        found = pattern.search(html)
        if found:
            return StatusResult(value, f"本文: {found.group(0)[:80]}")

    return StatusResult(UNKNOWN, "ページは開けたが、状態を書いた箇所が見つからない")


def find_mls(html: str) -> str | None:
    found = _MLS.search(html)
    return found.group(1).upper() if found else None


def check_url(config: Config, row: Row) -> str | None:
    """その物件で開けるURL。**販売ページを優先する。**

    記事（source_url）より、販売ページ（listing_url）のほうが状態を
    書いている。どちらも開けない相手なら None。
    """
    for key in ("listing_url", "source_url"):
        try:
            candidate = row[key]
        except (KeyError, IndexError, TypeError):
            candidate = None
        if can_check(config, candidate):
            return str(candidate)
    return None


def check(
    config: Config, row: Row, *, client: HttpClient
) -> StatusResult | None:
    """1件ぶん確かめる。開けない相手なら None（＝確認していない）。

    **開けなかったことを「販売していない」にしない。** どちらも
    「分からない」だが、意味がまったく違う。
    """
    url = check_url(config, row)
    if url is None:
        return None

    try:
        # 404 / 410 は「消えている」という答えなので、例外にせず受け取る。
        response = client.get(url, allow_status=(404, 410))
    except RobotsDisallowed:
        log.info("robots.txt により確認しません: %s", url)
        return None
    except Exception as exc:  # noqa: BLE001 - 1件で全体を止めない
        log.warning("販売ページを開けませんでした: %s (%s)", url, exc)
        return None

    if response.status_code in (404, 410):
        return StatusResult(
            OFF_MARKET, f"掲載ページが {response.status_code}（消えている）", url
        )

    result = detect(response.text)
    result.url = url
    result.mls = find_mls(response.text)
    return result


def save(conn: DbConnection, property_id: int, result: StatusResult | None) -> None:
    """結果を残す。**確認できなかったものは触らない。**"""
    if result is None:
        return
    conn.execute(
        "UPDATE properties SET listing_status = ?, listing_status_at = ?, "
        "listing_status_note = ?, mls_number = COALESCE(?, mls_number) WHERE id = ?",
        (
            result.value,
            datetime.now(UTC).isoformat(),
            f"{result.evidence}（{result.url}）" if result.url else result.evidence,
            result.mls,
            property_id,
        ),
    )
    conn.commit()


def targets(conn: DbConnection, limit: int | None = None) -> list[Row]:
    """確かめる価値のある行。**出す予定のあるものから。**

    未審査まで含めると数百件を毎回叩くことになる。承認・納品済みは
    これから投稿に回るので、そこが古い情報だと実害が出る。
    """
    sql = (
        "SELECT * FROM properties WHERE status IN ('approved', 'delivered') "
        "AND (source_url LIKE 'http%' OR listing_url LIKE 'http%') "
        "ORDER BY listing_status_at IS NOT NULL, listing_status_at, id DESC"
    )
    if limit:
        return conn.execute(f"{sql} LIMIT ?", (limit,)).fetchall()
    return conn.execute(sql).fetchall()


__all__ = [
    "ACTIVE",
    "LABELS",
    "OFF_MARKET",
    "PENDING",
    "SOLD",
    "UNKNOWN",
    "StatusResult",
    "blocked_domains",
    "can_check",
    "check",
    "check_url",
    "detect",
    "find_mls",
    "save",
    "targets",
]
