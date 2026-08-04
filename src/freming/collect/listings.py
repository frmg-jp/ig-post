"""経路A: 販売ソース（mode: crawl）からの収集。

仲介サイトは記事メディアと違ってRSSを持たないが、公開している
sitemap.xml に物件ページが並んでいる。そこを入口にする。

    sitemap（入れ子をたどる） → 物件URLの絞り込み → 詳細ページ → 候補化

経路Bとの違い:
  - 販売シグナルの検出をしない。物件ページはそもそも売出中の物件なので、
    本文から「売出中か」を推定する必要がない（is_for_sale=1 で入れる）。
  - 価格・所在地をページから直接取る。経路Bは記事本文からの推定だが、
    こちらは物件ページの構造化された表示から拾える。

取得は HttpClient を通すので、robots.txt・リクエスト間隔・同一ドメインの
直列化はここで気にしなくても守られる。

単体実行:
    python -m freming.collect.listings --source dreamtown --limit 5 --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from freming.collect.base import Candidate, normalize_url, parse_page
from freming.config import Config, ListingCrawl, ListingSource, load_config
from freming.db.connection import DbConnection, connect
from freming.db.repository import exists_source_url, insert_candidate
from freming.images.placeholder import is_flat_image
from freming.logging_setup import get_logger, setup_logging
from freming.net.client import HttpClient, RobotsDisallowed

log = get_logger(__name__)

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)

# 米国の住所表記。州の略号と郵便番号が並ぶところを錨にして、その手前を
# 市名、さらに手前を街路とみなす。実測した2サイトはどちらもこの形だった。
#
#   Dream Town  og:title  "11012 S Kilpatrick Avenue #2NE, Oak Lawn IL 60453"
#   Vanguard    本文       "95 Highland Way, Inverness, CA 94937 $500,000"
#
# URLの slug から市名を推測する手もあるが、"95-highland-way-inverness-ca"
# のように街路と市名の境が書式から判別できず、「Way Inverness」のような
# 取り違えが出た。表示されている住所を読む方が確かなのでこちらを使う。
_US_ADDRESS_RE = re.compile(
    r"([0-9][^,\n]{2,60}?),\s*([A-Za-z][A-Za-z .'\-]{1,38}?),?\s+([A-Z]{2})\s+(\d{5})(?:-\d{4})?"
)

# 台湾の住所。「縣市 + 區/鄉/鎮/市」がほぼ例外なく先頭に来る定型なので、
# 米国住所と同じやり方で錨にできる。
#
#   住商不動產    og:title  "台北市中山區台北市中正國小旁靜巷雅寓"
#   21世紀不動產  本文       "地址 新北市板橋區大明街"
#
# 第2階層に 市 を含めるのは 縣轄市（新竹縣竹北市 など）があるため。
# 会社の住所と衝突しないかは実測した。住商のフッターは
# 「台北市敦化南路二段267號3F之2」で 區 を書いておらず、この式に
# 一致しない。両サイトとも、ページ全体で一致する 縣市+區 の組は1種類だけ
# だった（住商 3箇所/1種類・21世紀 15箇所/1種類）。
_TW_ADDRESS_RE = re.compile(r"([一-鿿]{1,3}[縣市])([一-鿿]{1,4}[區鄉鎮市])")


@dataclass
class ListingStats:
    """1ソース分の収集結果。"""

    source: str
    seen_urls: int = 0            # sitemap / 一覧から拾ったURLの総数
    matched_urls: int = 0         # 物件URLとして通ったもの
    skipped_known: int = 0        # DBに既にあるもの
    fetched: int = 0              # 実際に取得した詳細ページ
    inserted: int = 0
    failed: int = 0
    disallowed: int = 0           # robots.txt で取得しなかったもの
    no_price: int = 0             # 価格を取れず候補にしなかったもの
    no_location: int = 0          # 所在地を取れず候補にしなかったもの
    no_photo: int = 0             # 写真が無い／プレースホルダだったもの
    samples: list[str] = field(default_factory=list)
    # 価格を取れなかったURL。価格の書式漏れなのか、そもそも物件ページで
    # ないのかを、推測せずに確かめられるようにする（--explain 用）。
    no_price_samples: list[str] = field(default_factory=list)
    # 所在地を取れなかったURL。抽出の書式漏れなのか、そもそもページに
    # 住所が無いのかを確かめられるようにする（--explain 用）。
    no_location_samples: list[str] = field(default_factory=list)
    # 写真が無かったURL。サイト側に写真が無いのか、抽出が外しているのかを
    # 確かめられるようにする（--explain 用）。
    no_photo_samples: list[str] = field(default_factory=list)

    def report(self) -> str:
        return (
            f"[{self.source}] URL {self.seen_urls}件 → 物件 {self.matched_urls}件 → "
            f"取得 {self.fetched}件 → 登録 {self.inserted}件"
            f"（既知 {self.skipped_known} / 価格なし {self.no_price} / "
            f"所在地なし {self.no_location} / 写真なし {self.no_photo} / "
            f"失敗 {self.failed} / robots拒否 {self.disallowed}）"
        )


def _addresses(text: str | None) -> list[tuple[str, str]]:
    """文字列に含まれる住所を、出現順にすべて返す。"""
    if not text:
        return []
    found = []
    for street, city, state, postal in _US_ADDRESS_RE.findall(text):
        found.append(
            (f"{street.strip()}, {city.strip()}, {state} {postal}", city.strip())
        )
    return found


def find_address(*texts: str | None) -> tuple[str, str] | None:
    """住所らしい並びを探し、(住所全体, 市名) を返す。

    渡された順に見て、最初に見つかったものを採る。og:title のように
    住所だけが入っている文字列を先に渡し、ページ全体は最後に回す。
    """
    for text in texts:
        found = _addresses(text)
        if found:
            return found[0]
    return None


def pick_address(url: str, title: str | None, page_text: str) -> tuple[str, str] | None:
    """物件の住所を選ぶ。会社の住所を掴まないようにする。

    仲介サイトはヘッダーとフッターに自社の住所を必ず置いている。単に
    最初の一致を採ると、どの物件も会社の所在地になる。

    実例（2026-08-03）: Nest Seekers はカナダ Wasaga Beach の物件に
    「New York」、Beverly Hills Estates は Bel Air の物件に
    「West Hollywood」が付いた。どちらも会社の住所。

    共通部分をタグで落とす手も試したが、Coldwell Banker は価格と住所を
    <header> の中に置いており、落とすと物件の情報まで消えた。
    そこで**URLと突き合わせる**。物件ページのURLには市名が入っている
    ことが多く、会社の住所はそこに出てこない。

    順に:
      1. og:title に住所があればそれ（住所だけが入っているので確実）
      2. ページ内の住所のうち、市名がURLに現れるもの
      3. 1件しか見つからないならそれ（会社の住所と紛れようがない）
      4. 決められなければ None。誤った所在地を入れるより空にする
    """
    from_title = _addresses(title)
    if from_title:
        return from_title[0]

    found = _addresses(page_text)
    if not found:
        return None

    slug = url.lower()
    for address, city in found:
        if city.lower().replace(" ", "-") in slug:
            return address, city

    unique_cities = {city for _, city in found}
    if len(unique_cities) == 1:
        return found[0]
    return None


def _tw_addresses(text: str | None) -> list[tuple[str, str]]:
    """台湾の住所を出現順に返す。(縣市+區, 縣市)。

    市は 台北市 のような直轄市を指す。區 まで含めた文字列は、審査UIで
    タイトルが無い物件の見出しに使う。
    """
    if not text:
        return []
    # 臺 と 台 はどちらも使われる（住商は「台北市」、21世紀は「臺北市」）。
    # 同じ市が2通りの表記でDBに入ると、集計も重点エリアの突き合わせも
    # ずれるので、常用の 台 に寄せる。
    return [
        (f"{c}{d}".replace("臺", "台"), c.replace("臺", "台"))
        for c, d in _TW_ADDRESS_RE.findall(text)
    ]


def pick_tw_address(title: str | None, page_text: str) -> tuple[str, str] | None:
    """台湾の物件ページから所在地を選ぶ。

    米国版（pick_address）はURLの slug に市名が出ることを頼りにしていたが、
    台湾の物件URLは数字のIDなので使えない。代わりに:

      1. og:title / <title> にあればそれ（住商は先頭が住所）
      2. 「地址」の直後にあるもの（21世紀はここに出る）
      3. ページ全体で 縣市 が1種類しかなければそれ
      4. 決められなければ None

    3 が効くのは、会社の住所が 區 を伴わない書き方（台北市敦化南路…）に
    なっていて、この式に掛からないため。掛かる書き方のサイトを足すときは
    ここで複数種類が出るので、None になって落ちる（誤った所在地は入らない）。
    """
    from_title = _tw_addresses(title)
    if from_title:
        return from_title[0]

    for match in re.finditer(r"地址[：:\s]*", page_text):
        tail = page_text[match.end() : match.end() + 40]
        found = _tw_addresses(tail)
        if found:
            return found[0]

    found = _tw_addresses(page_text)
    if not found:
        return None
    if len({city for _, city in found}) == 1:
        return found[0]
    return None


def _first_price(text: str, patterns: list[str]) -> str | None:
    """本文から売出価格を取る。

    同じ価格がページ内に何度も出る作りが多いので、最初の一致を採る。
    複数の異なる価格が出るページ（近隣物件の併記など）では取り違える
    余地が残るが、審査UIに価格を出しているので人が気づける。
    """
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0).strip()
    return None


class ListingCollector:
    def __init__(self, config: Config, client: HttpClient, conn: DbConnection) -> None:
        self.config = config
        self.client = client
        self.conn = conn

    # ------------------------------------------------------------------
    def _has_real_photo(self, thumbnail_url: str | None) -> bool:
        """代表画像が実際に絵として成立しているかを確かめる。

        取得できない・判定できないものは True を返す（通す）。ここは候補を
        落とすための判定なので、「確かに単色だった」と言えるときだけ落とす。
        相手サイトの一時的な不調で候補が消えるほうが害が大きい。
        """
        if not thumbnail_url:
            return False
        try:
            response = self.client.get(thumbnail_url)
        except RobotsDisallowed:
            # robots で画像が取れないだけでは物件を落とさない。
            return True
        except Exception as exc:  # noqa: BLE001 - 画像1枚の失敗で候補を消さない
            log.debug("代表画像を取得できません: %s (%s)", thumbnail_url, exc)
            return True
        return not is_flat_image(response.content, self.config.images.flat_stddev_max)

    # ------------------------------------------------------------------
    def _sitemap_urls(self, crawl: ListingCrawl) -> list[str]:
        """sitemap を段階的にたどって、物件URLの候補を集める。

        sitemap index が sitemap を指し、その先に物件が並ぶ、という
        二段構えが多い。物件URLに一致するものが出てきた段階で、その階層を
        結果として採り、それ以上は降りない（数万件の sitemap を全部
        読みに行かないため）。
        """
        frontier = list(crawl.sitemap_urls)
        collected: list[str] = []
        for _ in range(max(crawl.sitemap_depth, 1)):
            if not frontier:
                break
            next_frontier: list[str] = []
            for sitemap_url in frontier:
                try:
                    response = self.client.get(sitemap_url)
                except RobotsDisallowed:
                    log.warning("sitemap が robots.txt で拒否されました: %s", sitemap_url)
                    continue
                except Exception as exc:  # noqa: BLE001 - 1本の失敗で全体を止めない
                    log.warning("sitemap を取得できません: %s (%s)", sitemap_url, exc)
                    continue
                for loc in _LOC_RE.findall(response.text):
                    if crawl.detail_allowed(loc):
                        collected.append(loc)
                    elif loc.endswith(".xml") or ".xml" in loc:
                        next_frontier.append(loc)
            if collected:
                break
            frontier = next_frontier
        return collected

    def _index_urls(self, source: ListingSource, crawl: ListingCrawl) -> list[str]:
        """一覧ページのリンクから物件URLを拾う（sitemap を持たないサイト用）。"""
        found: list[str] = []
        for index_url in crawl.index_urls:
            try:
                response = self.client.get(index_url)
            except RobotsDisallowed:
                log.warning("一覧ページが robots.txt で拒否されました: %s", index_url)
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("一覧ページを取得できません: %s (%s)", index_url, exc)
                continue
            soup = BeautifulSoup(response.text, "lxml")
            base = source.base_url or index_url
            for anchor in soup.find_all("a", href=True):
                absolute = urljoin(base, anchor["href"].strip())
                if crawl.detail_allowed(absolute):
                    found.append(absolute)
        return found

    # ------------------------------------------------------------------
    def collect(
        self,
        source: ListingSource,
        limit: int | None = None,
        dry_run: bool = False,
        explain: bool = False,
    ) -> ListingStats:
        crawl = source.crawl
        if crawl is None:
            raise SystemExit(
                f"'{source.key}' に crawl の設定がありません（config.yaml の listing_sources）"
            )

        stats = ListingStats(source=source.key)
        urls = self._sitemap_urls(crawl) + self._index_urls(source, crawl)
        stats.seen_urls = len(urls)

        # 重複を落としつつ順序は保つ。sitemap と一覧ページの両方に
        # 同じ物件が出ることがある。
        seen: set[str] = set()
        targets: list[str] = []
        for url in urls:
            normalized = normalize_url(url)
            if normalized in seen or not crawl.detail_allowed(normalized):
                continue
            seen.add(normalized)
            targets.append(normalized)
        stats.matched_urls = len(targets)

        cap = limit if limit is not None else crawl.max_details
        for url in targets:
            if stats.fetched >= cap:
                break
            if exists_source_url(self.conn, url):
                stats.skipped_known += 1
                continue
            candidate = self._build(source, crawl, url, stats)
            if candidate is None:
                continue
            if explain and len(stats.samples) < 10:
                stats.samples.append(
                    f"  {candidate.price or '価格なし':>12}  "
                    f"{(candidate.location_city or '所在地不明'):<18} {candidate.title or url}"
                )
            if dry_run:
                continue
            if insert_candidate(self.conn, candidate) is not None:
                stats.inserted += 1
        if not dry_run:
            self.conn.commit()
        return stats

    def _build(
        self,
        source: ListingSource,
        crawl: ListingCrawl,
        url: str,
        stats: ListingStats,
    ) -> Candidate | None:
        try:
            response = self.client.get(url)
        except RobotsDisallowed:
            stats.disallowed += 1
            return None
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で残りを止めない
            log.warning("物件ページを取得できません: %s (%s)", url, exc)
            stats.failed += 1
            return None
        stats.fetched += 1

        page = parse_page(response.text, url)
        # 価格も住所も、parse_page が集める <p> の地の文には入っていない
        # ことが多い。物件ページでは見出しや専用のボックスに置かれる。
        # 記事メディアと違って関連記事の価格が混ざる作りではないので、
        # 価格はページ全体から拾ってよい。住所は会社のものと紛れるので
        # pick_address で選び分ける。
        whole_text = BeautifulSoup(response.text, "lxml").get_text(" ", strip=True)

        price = _first_price(page.text, crawl.price_patterns) or _first_price(
            whole_text, crawl.price_patterns
        )
        if price is None:
            # 価格の無いページは、売却済み・準備中・一覧ページの取り違えが
            # ほとんど。候補にすると審査の手間だけが増えるので入れない。
            stats.no_price += 1
            if len(stats.no_price_samples) < 10:
                stats.no_price_samples.append(url)
            return None

        address = None
        if crawl.location_from == "address":
            address = pick_address(url, page.title, whole_text)
        elif crawl.location_from == "tw_address":
            address = pick_tw_address(page.title, whole_text)

        if crawl.require_location and address is None:
            # 所在地の分からない物件は入れない。エリアはスコアの2割を占め、
            # 空だと採点が成り立たない。フッターの自社住所を拾って**誤った**
            # 所在地を入れるくらいなら空にする、という判断で pick_address は
            # 決められないとき None を返す。その None をここで落とす。
            stats.no_location += 1
            if len(stats.no_location_samples) < 10:
                stats.no_location_samples.append(url)
            return None

        if self.config.images.require_real_photo and not self._has_real_photo(
            page.thumbnail_url
        ):
            # **写真の無い物件は入れない。** 物件情報サイトは写真が用意
            # できていない物件にも og:image を返し、中身は単色の板になる
            # （Dream Town は 1280x800 の #D0D0D0）。寸法は本物と同じなので
            # min_short_edge_px では落ちず、審査UIに灰色の四角が並ぶ。
            # 建築を見せる企画で写真が無いものは、その時点で候補にならない。
            stats.no_photo += 1
            if len(stats.no_photo_samples) < 10:
                stats.no_photo_samples.append(url)
            return None

        # 見出しを持たない物件ページがある（Vanguard は <title> も og:title も
        # 空で、住所は本文にしかない）。URLをタイトルに据えると審査UIで
        # 何の物件か分からないので、住所を代わりに使う。
        title = page.title or (address[0] if address else None) or url

        return Candidate(
            source=source.key,
            source_url=url,
            source_rank=source.rank,
            title=title,
            thumbnail_url=page.thumbnail_url,
            content_text=page.text,
            # 経路Bと違い、売出中かどうかを推定していない。物件ページである
            # ことが根拠なので、そのまま記録する。
            for_sale_evidence=f"{source.name} の物件ページ",
            signal_score=None,
            price=price,
            location_city=address[1] if address else None,
            location_country=crawl.country,
            is_for_sale=1,
        )


# ----------------------------------------------------------------------
def collect_listing_source(
    config: Config,
    source_key: str,
    limit: int | None = None,
    dry_run: bool = False,
    explain: bool = False,
) -> ListingStats:
    source = config.listing_source(source_key)
    if source is None:
        raise SystemExit(f"listing_sources に '{source_key}' がありません")
    if source.mode != "crawl":
        raise SystemExit(
            f"'{source_key}' は mode: {source.mode} です。自動収集は行いません"
            "（審査UIの手動URL投入を使ってください）"
        )
    if not source.enabled and not dry_run:
        # dry-run は書き込まないので止めない。未検証のソースを
        # enabled: false のまま試せる状態にしておく。
        raise SystemExit(
            f"'{source_key}' は enabled: false です。config.yaml を確認するか、"
            "--dry-run で試してください"
        )

    conn = connect(config.app.target())
    try:
        with HttpClient(config.http) as client:
            return ListingCollector(config, client, conn).collect(
                source, limit, dry_run, explain
            )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="販売ソースから収集（経路A）")
    parser.add_argument("--source", required=True, help="listing_sources の key")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--explain", action="store_true", help="拾った物件を一覧表示")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    setup_logging(config.app.log_dir, config.app.log_level)
    stats = collect_listing_source(
        config, args.source, args.limit, args.dry_run, args.explain
    )
    print(stats.report())
    if args.explain and stats.samples:
        print("\n拾った物件:")
        print("\n".join(stats.samples))
    for label, samples in (
        ("価格を取れなかったURL", stats.no_price_samples),
        ("所在地を取れなかったURL", stats.no_location_samples),
        ("写真が無かったURL", stats.no_photo_samples),
    ):
        if args.explain and samples:
            print(f"\n{label}:")
            for url in samples:
                print(f"  {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
