"""収集の共通部品。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from freming.images.extract import image_urls_from_soup

_STRIP_TAGS = ("script", "style", "noscript", "svg", "form")

# ページの共通部分。ここを本文に混ぜると、記事の内容と関係なく
# 販売キーワードやリンクを拾ってしまう。
#
# 実例（2026-08-03）: thespaces.com はナビゲーションに "Property" があり、
# 全ページ共通の掲載枠に "for sale" と "listed for" が入っていた。
# その結果、美術展のまとめ記事もデザイン評論も、例外なく販売シグナル1点が
# 付いていた。全記事が同じ点数になり、シグナルが何の情報も持たない状態。
_CHROME_TAGS = ("nav", "aside", "footer")

# 本文が入っている要素の候補。見つかればその中だけを見る。
_ARTICLE_SELECTORS = (
    "article",
    "main",
    "[role='main']",
    ".entry-content",
    ".post-content",
    ".article-content",
    ".article-body",
)

# 記事要素の「内側」に混ざるノイズ。関連記事・人気記事・著者プロフィール・
# ニュースレター誘導など、記事ごとに内容が変わらないブロック。
#
# 実例（2026-08-03、Robb Report）: 見出しが $15.3 Million の記事から
# $9.3 Million が検出された。関連記事ブロックに並んだ別物件の価格を
# 拾っていた。1記事あたり10〜15件の金額が混ざり、複数の記事で同じ
# 本文が出ていた。nav/aside/footer を落とすだけでは取れない。
_NOISE_TOKENS = re.compile(
    r"(?:^|[-_\s])(?:"
    r"related|recirc\w*|recommend\w*|more-?stories|most-?recent|read-?more|"
    r"trending|popular|newsletter|subscribe|promo|advert\w*|"
    r"author-?bio|byline-?bio|share|social|comments?"
    r")(?:[-_\s]|$)",
    re.IGNORECASE,
)

# 本文とみなす最低の長さ。短すぎる要素を掴むと、本文をほとんど捨てて
# しまうので、その場合はページ全体に戻す。
_MIN_ARTICLE_CHARS = 400
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
    # 手動入力で人が直接与える項目（自動収集では未設定のままスコアリングに委ねる）
    price: str | None = None
    location_city: str | None = None
    location_country: str | None = None
    is_for_sale: int | None = None
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


def _strip_noise(node) -> None:
    """class / id から、記事ごとに内容が変わらないブロックを落とす。

    見出しの価格と検出される価格が食い違うのは、たいていこれが原因。
    関連記事に並んだ別物件の価格を拾っている。
    """
    targets = [
        el for el in node.find_all(True)
        if _NOISE_TOKENS.search(
            " ".join(el.get("class") or []) + " " + (el.get("id") or "")
        )
    ]
    for el in targets:
        # 入れ子で既に外れているものは触らない
        if el.parent is not None:
            el.decompose()


def _body_text(root) -> str:
    """記事の地の文だけを取り出す。

    class 名でノイズを除くやり方は、名前を当てられないと届かない。
    実例（2026-08-03、Robb Report）: 関連記事と著者の「最近の記事」一覧を
    class で落としきれず、山火事の避難記事に別記事の見出し
    「… Hits the Market for $3 Million」が混ざって売出中と判定された。
    3つの記事が同じキーワードと同じ価格を持つという形で表面化した。

    そこで構造で取る。記事の地の文は <p> に入り、関連記事の一覧は
    <li><a> や <h3><a> に入る。<p> だけを集めれば、class 名を知らなくても
    見出しの羅列は落ちる。

    <p> を使わずに本文を組む作りのページもあるので、集めた結果が短すぎる
    ときは全体に戻す（本文を丸ごと捨てるよりは、ノイズが混ざる方がまし）。
    """
    paragraphs = [p.get_text(" ", strip=True) for p in root.find_all("p")]
    text = " ".join(" ".join(paragraphs).split())
    if len(text) >= _MIN_ARTICLE_CHARS:
        return text
    return " ".join(root.get_text(separator=" ").split())


def _article_root(soup):
    """本文が入っている要素を返す。見つからなければページ全体。

    共通部分（ナビ・サイドバー・フッター）は必ず落とす。販売シグナルの
    検出は本文に対して行うものなので、ここが混ざると記事の内容と無関係に
    点が付く。
    """
    for selector in _ARTICLE_SELECTORS:
        node = soup.select_one(selector)
        if node is None:
            continue
        for tag in node(_CHROME_TAGS):
            tag.decompose()
        _strip_noise(node)
        if len(node.get_text(strip=True)) >= _MIN_ARTICLE_CHARS:
            return node

    # 本文要素が見つからないページ（フィード配信のHTML断片など）は
    # 共通部分だけ落として全体を使う。
    for tag in soup(_CHROME_TAGS):
        tag.decompose()
    _strip_noise(soup)
    return soup.body or soup


def parse_page(html: str, base_url: str, skip_lead_image: bool = False) -> PageContent:
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

    root = _article_root(soup)

    # 見出しは本文の一部として扱う。共通部分を落とすと <header> ごと
    # 消えることがあり、「Home for Sale in ...」のような見出しの
    # キーワードを取りこぼすため、先頭に明示的に付ける。
    body = _body_text(root)
    text = " ".join(part for part in (title, body) if part)[:_MAX_TEXT_CHARS]

    # リンクも本文の中だけから拾う。フッターやサイドバーに仲介サイトへの
    # リンクがあると、記事の内容と無関係に「売出中の裏付け」になってしまう。
    links: list[str] = []
    for anchor in root.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        absolute = urljoin(base_url, href)
        if absolute not in links:
            links.append(absolute)

    # 代表画像は納品とまったく同じ選び方を通す。以前ここに別の実装を持って
    # いたが、遅延読み込み（data-src / srcset）とノイズ除外が抜けており、
    # プレースホルダの透明画像を選んで「画像が表示されない」状態になった。
    #
    # image_urls_from_soup は渡された soup からタグを落とすので、本文と
    # リンクを取り出したあとに呼ぶ（先に呼ぶと本文が欠ける）。
    images = image_urls_from_soup(soup, base_url, limit=2, skip_lead_image=skip_lead_image)
    thumbnail = images[0] if images else None
    return PageContent(title=title, text=text, links=links, thumbnail_url=thumbnail)
