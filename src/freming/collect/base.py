"""収集の共通部品。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

_STRIP_TAGS = ("script", "style", "noscript", "svg", "form")
_MAX_TEXT_CHARS = 20000


@dataclass
class Candidate:
    """収集直後の候補。スコアリング前なので score 系は未設定。"""

    source: str
    source_url: str
    source_rank: str | None = None
    title: str | None = None
    thumbnail_url: str | None = None
    content_text: str | None = None
    for_sale_evidence: str | None = None
    signal_score: int | None = None
    collected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class PageContent:
    title: str | None
    text: str
    links: list[str]
    thumbnail_url: str | None


def normalize_url(url: str) -> str:
    """フラグメントを除去し、末尾のスラッシュ差分を吸収する。

    同じ記事が別URLとして二重登録されるのを防ぐ。
    """
    url, _ = urldefrag(url.strip())
    parsed = urlparse(url)
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return parsed._replace(path=path).geturl()


def parse_page(html: str, base_url: str) -> PageContent:
    """HTMLから本文・リンク・サムネイルを取り出す。"""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    title = None
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()

    thumbnail = None
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        thumbnail = urljoin(base_url, og_image["content"].strip())

    text = " ".join(soup.get_text(separator=" ").split())[:_MAX_TEXT_CHARS]

    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        absolute = urljoin(base_url, href)
        if absolute not in links:
            links.append(absolute)

    return PageContent(title=title, text=text, links=links, thumbnail_url=thumbnail)
