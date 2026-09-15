"""[4] 枚数が足りない物件の画像を、**他のサイトから足す**。

    手元の写真 → 画像検索 → 同じ写真が載っている他のページ → そこの写真

掲載ページに3枚しか無い物件は、その記事を何度読み直しても3枚のまま
（refetch-images は同じページを読み直すだけ）。同じ物件は他の媒体でも
たいてい取り上げられていて、そちらには別の写真がある。

探し方は2つあり、上から順に試す:

  1. **画像検索**（Cloud Vision の Web Detection）。いま持っている写真を
     渡して、その写真が載っているページを教えてもらう。**同じ物件である
     確証がいちばん強い**（同じ写真なのだから）。
  2. **名前と住所での検索**（Custom Search JSON API）。1で何も出なかった
     ときの当て。鍵が未設定なら黙って飛ばす。こちらは同姓同名・別物件を
     拾いうるので、結果は画像検索のうしろに置く。

**見つけたページも、従来どおりの作法でしか開かない。** robots.txt を
確認し、ドメインごとに間隔をあける（HttpClient 任せ）。自動収集が
禁止されているサイトは config の blocked_domains に入れてあり、検索結果に
出てきても開かない。

取ってきた画像には `images.origin_url` に**そのページのURL**を残す。
元の記事の写真と混ざったまま見分けが付かないと、審査で「この写真は
どこの？」に答えられない。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

from freming.config import Config, ImageDiscoveryConfig
from freming.db.connection import DbConnection, Row
from freming.images.extract import extract_image_urls
from freming.images.fetch import FetchStats, ingest_urls
from freming.logging_setup import get_logger
from freming.net.client import HttpClient, RobotsDisallowed

log = get_logger(__name__)

VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
SEARCH_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"

# Web Detection の単価（2026-09 時点）。毎月1000回までは無料。
# 見積もりを出すためだけに持つ。請求の正は Google 側。
VISION_UNIT_USD = 0.0035

# 1ドメインから開くページ数の上限。同じサイトの関連記事が検索結果を
# 埋めることがあり、そのまま回すと1物件で同じ相手を何度も叩く。
MAX_PAGES_PER_DOMAIN = 2


class DiscoveryError(RuntimeError):
    """画像検索が使えない（認証・APIの有効化・通信）。"""


@dataclass
class PageHit:
    """同じ物件が載っていそうなページ。"""

    url: str
    title: str | None = None
    how: str = "画像検索"


@dataclass
class FillStats:
    property_id: int
    had: int = 0
    vision_calls: int = 0
    pages_found: int = 0
    pages_rejected: int = 0   # この物件のページだと確かめられなかった
    pages_opened: int = 0
    gained: int = 0
    now: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"[画像補充] property_id={self.property_id} "
            f"{self.had} → {self.now} 枚"
            f"（候補ページ {self.pages_found} / 開いた {self.pages_opened} / "
            f"別物件として外した {self.pages_rejected} / "
            f"画像検索 {self.vision_calls} 回）"
        )


# ----------------------------------------------------------------------
# 認証
# ----------------------------------------------------------------------
def access_token(cfg: ImageDiscoveryConfig) -> str:
    """Vision を叩くためのアクセストークン。

    Drive（delivery/drive.py）と同じ考え方で、鍵ファイルを持たない
    経路（ADC / Workload Identity 連携）を既定にする。**スコープは
    cloud-platform。** Drive のスコープだけでは Vision は 403 になる。
    """
    import google.auth
    from google.auth.transport.requests import Request as AuthRequest
    from google.oauth2 import service_account

    try:
        if cfg.auth_mode == "service_account":
            if not cfg.credentials_path.exists():
                raise DiscoveryError(
                    f"サービスアカウント鍵が見つかりません: {cfg.credentials_path}"
                )
            creds = service_account.Credentials.from_service_account_file(
                str(cfg.credentials_path), scopes=[CLOUD_PLATFORM]
            )
        else:
            creds, _project = google.auth.default(scopes=[CLOUD_PLATFORM])
        creds.refresh(AuthRequest())
    except DiscoveryError:
        raise
    except Exception as exc:  # 原因を日本語にして上げ直す
        raise DiscoveryError(
            "Google の資格情報を取得できませんでした。\n"
            "  手元なら: gcloud auth application-default login "
            f'--scopes="{CLOUD_PLATFORM}"\n'
            f"  ({exc})"
        ) from exc
    token = getattr(creds, "token", None)
    if not token:
        raise DiscoveryError("アクセストークンが空でした。認証設定を確認してください。")
    return str(token)


# ----------------------------------------------------------------------
# 探す
# ----------------------------------------------------------------------
def matching_pages(
    cfg: ImageDiscoveryConfig, image_urls: list[str], *, token: str, timeout: float = 60.0
) -> list[PageHit]:
    """その写真が載っている他のページ（Cloud Vision の Web Detection）。

    **1枚につき1回の課金。** 呼ぶ前に枚数を絞ること（probe_images）。

    画像は Google 側が取りに来るので、渡せるのは公開URLだけ。手元の
    ファイルパスは渡せない（images.source_url は取得元のURLなのでそのまま
    使える）。
    """
    import httpx

    if not image_urls:
        return []
    body = {
        "requests": [
            {
                "image": {"source": {"imageUri": url}},
                "features": [{"type": "WEB_DETECTION", "maxResults": 20}],
            }
            for url in image_urls
        ]
    }
    response = httpx.post(
        VISION_ENDPOINT,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if response.status_code != 200:
        detail = ""
        try:
            detail = str(response.json().get("error", {}).get("message") or "")
        except ValueError:
            detail = response.text[:300]
        raise DiscoveryError(
            f"Vision API が {response.status_code} を返しました: {detail}"
        )

    hits: list[PageHit] = []
    for entry in response.json().get("responses", []):
        error = entry.get("error")
        if error:
            # 1枚が読めなくても残りは使える（画像が消えている・403 など）。
            log.info("画像検索が1枚ぶん失敗しました: %s", error.get("message"))
            continue
        detection = entry.get("webDetection") or {}
        for page in detection.get("pagesWithMatchingImages") or []:
            url = page.get("url")
            if url:
                hits.append(PageHit(url=url, title=page.get("pageTitle")))
    return hits


def search_pages(
    cfg: ImageDiscoveryConfig, query: str, *, limit: int = 5, timeout: float = 30.0
) -> list[PageHit]:
    """物件名・住所での検索（Custom Search JSON API）。

    **鍵が無ければ何もしない。** 画像検索（Vision）とは別のAPIで、別に
    有効化と鍵が要る。無いまま落とすと、画像検索だけで足りている場合まで
    失敗扱いになる。
    """
    import httpx

    key = os.environ.get(cfg.search_api_key_env)
    engine = os.environ.get(cfg.search_engine_id_env)
    if not key or not engine:
        log.info(
            "名前・住所での検索は設定されていないので飛ばします"
            "（%s と %s が要ります）",
            cfg.search_api_key_env, cfg.search_engine_id_env,
        )
        return []
    if not query.strip():
        return []

    response = httpx.get(
        SEARCH_ENDPOINT,
        params={"key": key, "cx": engine, "q": query, "num": min(limit, 10)},
        timeout=timeout,
    )
    if response.status_code != 200:
        log.warning("検索が %s を返しました: %s", response.status_code, response.text[:200])
        return []
    return [
        PageHit(url=item["link"], title=item.get("title"), how="名前検索")
        for item in response.json().get("items", [])
        if item.get("link")
    ]


# ----------------------------------------------------------------------
# 絞り込み
# ----------------------------------------------------------------------
def _normalize(url: str) -> str:
    """比較用。断片（#...）だけ落とす。クエリは残す（別ページのことがある）。"""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_blocked(cfg: ImageDiscoveryConfig, url: str) -> bool:
    """自動収集が禁止されているサイトか。**こちらから開かない。**"""
    host = _domain(url)
    return any(
        host == blocked or host.endswith("." + blocked)
        for blocked in (b.lower() for b in cfg.blocked_domains)
    )


def usable_pages(
    cfg: ImageDiscoveryConfig, hits: list[PageHit], *, known: set[str]
) -> list[PageHit]:
    """開いてよいページだけを、重複を除いて並べる。

    known には掲載ページ・販売ページ・前に開いたページを入れる。同じ
    ページを二度開かないため。
    """
    seen = {_normalize(url) for url in known}
    per_domain: dict[str, int] = {}
    out: list[PageHit] = []
    for hit in hits:
        url = _normalize(hit.url)
        if not url.startswith("http") or url in seen:
            continue
        if is_blocked(cfg, url):
            log.info("自動収集が禁止されているサイトなので開きません: %s", _domain(url))
            seen.add(url)
            continue
        host = _domain(url)
        if per_domain.get(host, 0) >= MAX_PAGES_PER_DOMAIN:
            continue
        per_domain[host] = per_domain.get(host, 0) + 1
        seen.add(url)
        out.append(PageHit(url=url, title=hit.title, how=hit.how))
        if len(out) >= cfg.max_pages_per_property:
            break
    return out


def _value(row: Row, key: str) -> str:
    try:
        return str(row[key] or "")
    except (KeyError, IndexError, TypeError):
        return ""


# 物件名にも住所にも出るが、**どの物件にも出る**語。手がかりにならない。
_GENERIC = frozenset({
    "home", "homes", "house", "residence", "property", "properties", "estate",
    "real", "listing", "listings", "for", "sale", "sold", "photo", "photos",
    "the", "and", "with", "this", "that", "from", "your", "new", "old",
    "street", "st", "avenue", "ave", "road", "rd", "drive", "dr", "lane", "ln",
    "court", "ct", "place", "pl", "boulevard", "blvd", "way", "circle",
    "north", "south", "east", "west", "n", "s", "e", "w", "unit", "apt",
    "modern", "midcentury", "century", "mid", "design", "architecture",
    "tour", "video", "watch", "trailer", "movie", "film",
})


def _words(text: str) -> set[str]:
    """比較用に、英数字の連なりだけを取り出す。URLのハイフンも区切り。"""
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if w}


def _identity(row: Row) -> tuple[str, set[str]]:
    """その物件を**名指しできる**手がかり。(番地, 語) を返す。"""
    street = _value(row, "street_address")
    number = ""
    for word in _words(street):
        if word.isdigit() and len(word) >= 3:
            number = word
            break
    terms = set()
    for source in (_value(row, "display_name"), _value(row, "title"), street):
        terms |= {w for w in _words(source) if not w.isdigit() and w not in _GENERIC and len(w) >= 3}
    return number, terms


def match_score(row: Row, hit: PageHit) -> int:
    """そのページが**この物件のページか**を点にする。

    **これが無いと、まったく関係ないページの写真を足す。** 2026-09-15 の
    初回実行で実際に起きた: property 58（6922 N Owen Avenue）の写真を
    画像検索にかけたところ、Disney の予告編ページが候補に出てきて、
    そこに並んでいた映画のスチルを3枚取り込んだ。画像検索は「似た画像が
    あるページ」を返すだけで、**同じ物件だとは言っていない。**

      - 番地（6922 のような数字）が出てくる: +2。いちばん強い
      - 物件名・通り名の語が出てくる: 6文字以上なら +2、短ければ +1
      - どの物件にも出る語（house / avenue / north …）は数えない

    2点で通す。teamfallico.com/properties/12719651/6922-n-owen-avenue は
    番地で通り、video.disney.com/watch/the-north-avenue-irregulars は
    0点で落ちる（north も avenue も手がかりにならない語）。
    """
    number, terms = _identity(row)
    found = _words(hit.url) | _words(hit.title or "")
    score = 2 if number and number in found else 0
    for term in terms:
        if term in found:
            score += 2 if len(term) >= 6 else 1
    return score


# ページを開いてよいと判断する点数。
MATCH_MIN = 2


def search_query(row: Row) -> str:
    """名前・住所での検索に使う文字列。"""
    parts = [
        _value(row, "display_name") or _value(row, "title"),
        _value(row, "street_address"),
        _value(row, "location_city"),
    ]
    return " ".join(p for p in parts if p).strip()


def probe_urls(conn: DbConnection, property_id: int, limit: int) -> list[str]:
    """画像検索にかける写真。**公開URLのものだけ。**

    Google 側が取りに来るので、手元のファイルは渡せない。先頭から採る
    （position 順＝記事の並び順で、その物件の代表的な写真が前に来る）。
    """
    rows = conn.execute(
        "SELECT source_url FROM images WHERE property_id = ? "
        "ORDER BY position, id", (property_id,),
    ).fetchall()
    return [r["source_url"] for r in rows if str(r["source_url"]).startswith("http")][:limit]


def image_count(conn: DbConnection, property_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM images WHERE property_id = ?", (property_id,)
    ).fetchone()["n"]


def undo_fill(conn: DbConnection, property_id: int) -> list[Row]:
    """**他サイトから足した画像だけ**を取り消す。元の記事の写真は残す。

    見当違いのページから取ってしまったときの戻し道。origin_url が
    入っている行だけを消すので、掲載ページの写真には触らない。

    image_skips は消さない。**もう一度同じURLを取りに行かないため。**
    消したい理由が「関係ない写真だった」なら、二度と取らないのが正しい。
    """
    rows = conn.execute(
        "SELECT id, source_url, local_path, origin_url FROM images "
        "WHERE property_id = ? AND origin_url IS NOT NULL ORDER BY position",
        (property_id,),
    ).fetchall()
    for row in rows:
        path = row["local_path"]
        if path:
            from contextlib import suppress
            from pathlib import Path

            with suppress(OSError):
                Path(path).unlink(missing_ok=True)
        conn.execute("DELETE FROM images WHERE id = ?", (row["id"],))
    conn.commit()
    return rows


def visited_origins(conn: DbConnection, property_id: int) -> set[str]:
    """前に開いた他サイトのページ。二度開かないため。"""
    rows = conn.execute(
        "SELECT DISTINCT origin_url FROM images WHERE property_id = ? "
        "AND origin_url IS NOT NULL", (property_id,),
    ).fetchall()
    return {str(r["origin_url"]) for r in rows}


# ----------------------------------------------------------------------
# 足す
# ----------------------------------------------------------------------
def fill_property(
    config: Config,
    conn: DbConnection,
    row: Row,
    *,
    client: HttpClient,
    token: str,
) -> FillStats:
    """1物件ぶん、他のサイトから画像を足す。**既存は消さない。**

    上限（images.max_per_property）に届いた時点で切り上げる。届かなくても
    そこで終わり——**枚数が揃わないことは投稿を止める理由にしない**
    （1枚でも出す）。
    """
    cfg = config.images.discovery
    property_id = int(row["id"])
    stats = FillStats(property_id=property_id)
    stats.had = stats.now = image_count(conn, property_id)

    need = config.images.max_per_property - stats.had
    if need <= 0:
        stats.notes.append("もう上限まであります")
        return stats

    hits: list[PageHit] = []
    probes = probe_urls(conn, property_id, cfg.probe_images)
    if probes:
        hits.extend(matching_pages(cfg, probes, token=token))
        stats.vision_calls = len(probes)
    else:
        # 1枚も無い物件は画像検索の起点が無い。名前と住所で探すしかない。
        stats.notes.append("手元に写真が無いので画像検索は使えません")

    query = search_query(row)
    if len(hits) < cfg.max_pages_per_property and query:
        hits.extend(search_pages(cfg, query))

    known = {_value(row, "source_url"), _value(row, "listing_url")}
    known |= visited_origins(conn, property_id)

    # **この物件のページだと言い切れないものは開かない。** 画像検索は
    # 「似た画像が載っているページ」を返すだけで、同じ物件だとは
    # 言っていない（match_score の説明を参照）。
    number, terms = _identity(row)
    if not number and not terms:
        stats.notes.append("名前も住所も無いので、ページを見分けられません")
        return stats
    named = [hit for hit in hits if match_score(row, hit) >= MATCH_MIN]
    stats.pages_rejected = len(hits) - len(named)
    for hit in hits:
        if match_score(row, hit) < MATCH_MIN:
            log.info("この物件のページか確かめられないので開きません: %s", hit.url)

    pages = usable_pages(cfg, named, known={u for u in known if u})
    stats.pages_found = len(pages)

    for page in pages:
        if image_count(conn, property_id) >= config.images.max_per_property:
            break
        try:
            article = client.get(page.url)
        except RobotsDisallowed:
            log.info("robots.txt により開きません: %s", page.url)
            continue
        except Exception as exc:  # noqa: BLE001 - 1ページで全体を止めない
            log.warning("ページを開けませんでした: %s (%s)", page.url, exc)
            continue
        stats.pages_opened += 1
        urls = extract_image_urls(article.text, page.url)
        if not urls:
            continue
        ingest_urls(
            config, conn, row, urls, client,
            FetchStats(property_id=property_id), origin_url=page.url,
        )

    stats.now = image_count(conn, property_id)
    stats.gained = stats.now - stats.had
    log.info(stats.summary())
    return stats


__all__ = [
    "MATCH_MIN",
    "VISION_UNIT_USD",
    "DiscoveryError",
    "FillStats",
    "PageHit",
    "access_token",
    "fill_property",
    "image_count",
    "is_blocked",
    "match_score",
    "matching_pages",
    "probe_urls",
    "search_pages",
    "search_query",
    "undo_fill",
    "usable_pages",
    "visited_origins",
]
