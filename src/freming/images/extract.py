"""記事HTMLから、掲載候補になる画像URLを取り出す。

記事には物件写真以外の画像（ロゴ、著者アイコン、広告、SNSボタン、
関連記事のサムネイル）が大量に混ざる。ダウンロードしてから捨てるのは
相手サイトへの無駄なリクエストになるので、URLの段階で落とせるものは
落としておく。
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# ファイル名・パスに現れたら物件写真ではないと判断する語。
_NOISE = re.compile(
    r"(logo|icon|avatar|sprite|badge|banner|advert|placeholder|"
    r"favicon|share|social|thumb(nail)?[-_/]?(small|xs)|1x1|spacer|pixel)",
    re.IGNORECASE,
)

# 画像として扱う拡張子。クエリ付きURLもあるのでパス部分だけを見る。
_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

# 記事本文が入っている可能性が高い要素。ここから探して、
# 見つからなければページ全体に広げる。
_ARTICLE_SELECTORS = ("article", "main", "[itemprop='articleBody']", ".entry-content")


def _is_image_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(_EXTENSIONS)


def _largest_from_srcset(srcset: str, base_url: str) -> str | None:
    """srcset から最も幅の大きい候補を選ぶ。

    レスポンシブ画像では src に小さい版が入っていることが多く、
    そのまま取ると min_short_edge_px を満たさず捨てることになる。
    """
    best: tuple[float, str] | None = None
    for part in srcset.split(","):
        chunk = part.strip().split()
        if not chunk:
            continue
        url = urljoin(base_url, chunk[0])
        width = 0.0
        if len(chunk) > 1 and chunk[1].endswith("w"):
            try:
                width = float(chunk[1][:-1])
            except ValueError:
                width = 0.0
        if best is None or width > best[0]:
            best = (width, url)
    return best[1] if best else None


def extract_image_urls(html: str, base_url: str, limit: int = 30) -> list[str]:
    """記事から画像URLを順番どおりに取り出す。

    並び順は記事に載っている順のまま返す。編集者が組んだ順序が
    そのまま「1枚目に何を置くか」の手がかりになるため、並べ替えない。
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(("script", "style", "noscript", "header", "footer", "nav", "aside")):
        tag.decompose()

    scope = None
    for selector in _ARTICLE_SELECTORS:
        scope = soup.select_one(selector)
        if scope is not None:
            break
    scope = scope or soup

    urls: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str | None) -> None:
        if not candidate or len(urls) >= limit:
            return
        if _NOISE.search(candidate) or not _is_image_url(candidate):
            return
        if candidate in seen:
            return
        seen.add(candidate)
        urls.append(candidate)

    # og:image は編集側が「代表」として指定した1枚なので先頭に置く
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        _add(urljoin(base_url, og_image["content"].strip()))

    for img in scope.find_all("img"):
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            _add(_largest_from_srcset(srcset, base_url))
            continue
        # 遅延読み込みでは src がプレースホルダのことがある
        for attr in ("data-src", "data-original", "data-lazy-src", "src"):
            value = img.get(attr)
            if value:
                _add(urljoin(base_url, value.strip()))
                break

    return urls
