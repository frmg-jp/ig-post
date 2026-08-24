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


# 同じ写真がURL違いで複数回出てくるパターン。WordPress系のサイトでは
# アップロード時に複数サイズが生成され、og:image は原寸、本文中は
# リサイズ版、ということが起きる。URL文字列の一致だけで重複を判定すると
# 同じ写真を2枚納品してしまう。
_SIZE_SUFFIX = re.compile(r"-\d{2,5}x\d{2,5}(?=\.[a-z]{3,4}$)", re.IGNORECASE)
_SCALE_SUFFIX = re.compile(r"[@_-][23]x(?=\.[a-z]{3,4}$)", re.IGNORECASE)
_WP_SCALED = re.compile(r"-scaled(?=\.[a-z]{3,4}$)", re.IGNORECASE)


def _is_image_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(_EXTENSIONS)


# `<script>` の中のJSONに入っている画像URL。
#
# **物件サイトのギャラリーは遅延読み込みで、HTMLの `<img>` には最初の
# 数枚しか出ない。** 残りは埋め込みJSONに入っている。
#
# 実例（2026-08-24、Coldwell Banker の 209 Java Drive）: 掲載写真は19枚
# あるのに3枚しか取れていなかった。og:image の1枚と `<img>` の2枚だけで、
# 残り16枚は `{"indexNum":3,"mediaUrl":"https://…_P03.jpg"}` の形で
# `<script>` の中にあった。抽出前に script を捨てていたので見えなかった。
#
# JSON の中では `/` が `\/` と書かれることがあるので戻してから探す。
_JSON_IMAGE = re.compile(
    r"https?://[^\s\"'<>\\]+\.(?:jpg|jpeg|png|webp)(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE,
)


def _urls_in_scripts(soup: BeautifulSoup) -> list[str]:
    """`<script>` の中に書かれた画像URLを、出てくる順に返す。

    **script を捨てる前に呼ぶこと。** 順序は原文のままにする。ギャラリーは
    たいてい掲載順に並んでいるので、そのまま投稿の並びに使える。
    """
    found: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all("script"):
        text = tag.string or tag.get_text() or ""
        if "http" not in text:
            continue
        for match in _JSON_IMAGE.finditer(text.replace("\\/", "/")):
            url = match.group(0)
            if url not in seen:
                seen.add(url)
                found.append(url)
    return found


def photo_identity(url: str) -> tuple[str, str]:
    """同じ写真なら同じ値になるキー。

    サイズ違い（-1600x1067）、倍率違い（@2x）、WordPress の -scaled、
    クエリ文字列、スキームの差を落とす。これらは同じ写真の別表現なので、
    どれか1枚だけを採る。
    """
    parsed = urlparse(url)
    path = parsed.path.lower()
    path = _SIZE_SUFFIX.sub("", path)
    path = _SCALE_SUFFIX.sub("", path)
    path = _WP_SCALED.sub("", path)
    return parsed.netloc.lower(), path


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


def extract_image_urls(
    html: str,
    base_url: str,
    limit: int = 30,
    skip_lead_image: bool = False,
) -> list[str]:
    """記事から画像URLを順番どおりに取り出す。

    並び順は記事に載っている順のまま返す。編集者が組んだ順序が
    そのまま「1枚目に何を置くか」の手がかりになるため、並べ替えない。
    """
    soup = BeautifulSoup(html, "lxml")
    return image_urls_from_soup(soup, base_url, limit, skip_lead_image)


def image_urls_from_soup(
    soup,
    base_url: str,
    limit: int = 30,
    skip_lead_image: bool = False,
) -> list[str]:
    """解析済みのHTMLから画像URLを取り出す。

    審査UIのサムネイルもここを通す。以前はサムネイル用に別の実装を持って
    いたが、遅延読み込み（data-src / srcset）とノイズ除外が抜けており、
    プレースホルダの透明画像を選んで「画像が表示されない」状態になった。
    選び方は1か所に置く。

    skip_lead_image を立てると先頭の1枚を落とす。物件写真に人物の顔写真を
    丸く重ねた合成画像を代表に据えるメディアがあり（Robb Report のセレブ
    記事）、そのままでは 01.jpg がその合成画像になるため。
    残り1枚しかない場合は落とさない（合成画像でも無いよりはまし）。
    """
    # **script を捨てる前に**、中のJSONに入っている画像URLを控える。
    # 遅延読み込みのギャラリーはここにしか無い（_urls_in_scripts）。
    script_urls = _urls_in_scripts(soup)

    for tag in soup(("script", "style", "noscript", "header", "footer", "nav", "aside")):
        tag.decompose()

    scope = None
    for selector in _ARTICLE_SELECTORS:
        scope = soup.select_one(selector)
        if scope is not None:
            break
    scope = scope or soup

    urls: list[str] = []
    seen: set[tuple[str, str]] = set()

    def _add(candidate: str | None) -> None:
        if not candidate or len(urls) >= limit:
            return
        if _NOISE.search(candidate) or not _is_image_url(candidate):
            return
        # URL文字列ではなく「同じ写真か」で重複を判定する
        key = photo_identity(candidate)
        if key in seen:
            return
        seen.add(key)
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

    # HTMLに出ていたものを優先し、足りない分をJSONから足す。**代表と
    # 本文の並びを崩さない**ため、この順にしてある。
    for candidate in script_urls:
        _add(urljoin(base_url, candidate))

    if skip_lead_image and len(urls) > 1:
        return urls[1:]
    return urls
