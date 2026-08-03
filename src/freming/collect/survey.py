"""収集候補サイトの robots.txt を一括で調べる。

判定の材料のうち、機械で確かめられるのは robots.txt の層だけ。
**利用規約と画像の権利は人が読んで判断する**（このツールは何も言わない）。
そこを混ぜると「robots がOK＝収集してよい」と誤読されるので、
出力にも毎回その断りを出す。

単体実行:
    python -m freming.collect.survey --file docs/source-candidates.tsv
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from freming.config import Config, load_config
from freming.logging_setup import get_logger, setup_logging
from freming.net.client import HttpClient
from freming.net.ratelimit import domain_of

log = get_logger(__name__)

# トップだけ見て「許可」と判断すると、物件ページ配下だけ Disallow の
# サイトを取りこぼす。実際に取りに行くことになるパスの形を試す。
PROBE_PATHS = ["/", "/search", "/listing", "/property", "/properties", "/homes"]


@dataclass
class SiteSurvey:
    name: str
    url: str
    robots_found: str          # ok / なし(404) / 取得失敗
    top_allowed: str           # 可 / 不可 / 不明
    blocked_paths: list[str]
    crawl_delay: float | None
    feeds: list[str]
    note: str = ""

    @property
    def verdict(self) -> str:
        """robots の層だけの判定。規約・画像権利はここに含めない。"""
        if self.robots_found == "取得失敗":
            return "×(robots取得不可)"
        if self.top_allowed == "不可":
            return "×(robots)"
        if self.blocked_paths:
            return "△(一部不可)"
        return "○(robotsのみ)"

    def row(self) -> list[str]:
        return [
            self.name,
            self.url,
            self.verdict,
            self.robots_found,
            self.top_allowed,
            ",".join(self.blocked_paths),
            f"{self.crawl_delay:g}" if self.crawl_delay is not None else "",
            str(len(self.feeds)),
            ";".join(self.feeds[:3]),
            self.note,
        ]


HEADER = [
    "サイト名", "URL", "robots判定", "robots.txt", "トップ",
    "不可パス", "Crawl-delay", "フィード数", "フィード", "備考",
]


def survey_site(config: Config, name: str, url: str, http: HttpClient) -> SiteSurvey:
    """1サイト分を調べる。相手には robots.txt とトップページしか触らない。"""
    rules = http.robots.rules_for(url)
    if not rules.fetched:
        return SiteSurvey(name, url, "取得失敗", "不明", [], None, [],
                          "robots.txt を読めないので取得は行わない（安全側に倒す）")
    robots_found = "なし(404)" if rules.missing else "ok"

    ua = config.http.user_agent
    top_allowed = "可" if rules.allows(url, ua) else "不可"
    blocked = [
        path for path in PROBE_PATHS[1:]
        if not rules.allows(urljoin(url, path), ua)
    ]

    feeds: list[str] = []
    note = ""
    if top_allowed == "可":
        # フィードの宣言だけ見る。記事本文の取得はしない。
        try:
            from freming.collect.editorial import discover_feeds

            feeds = [feed for feed, _label in discover_feeds(config, url)]
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で残りを止めない
            note = f"フィード探索に失敗: {type(exc).__name__}"

    return SiteSurvey(
        name, url, robots_found, top_allowed, blocked,
        rules.crawl_delay(ua), feeds, note,
    )


def load_candidates(path: Path) -> list[tuple[str, str, str]]:
    """エリア / サイト名 / URL の3列を読む（TSV）。先頭行は見出し。"""
    rows: list[tuple[str, str, str]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in csv.reader(f, delimiter="\t"):
            if len(line) < 3 or line[2].strip().startswith(("URL", "#")):
                continue
            area, name, url = line[0].strip(), line[1].strip(), line[2].strip()
            if url.startswith("http"):
                rows.append((area, name, url))
    return rows


def survey(config: Config, sites: list[tuple[str, str, str]]) -> list[SiteSurvey]:
    """順に調べる。同一ドメインへの並列アクセスはしない（HttpClient が直列化する）。"""
    results: list[SiteSurvey] = []
    http = HttpClient(config.http)
    try:
        seen: set[str] = set()
        for area, name, url in sites:
            domain = domain_of(url)
            if domain in seen:
                log.info("同じドメインなのでスキップ: %s", url)
                continue
            seen.add(domain)
            result = survey_site(config, f"{area} / {name}", url, http)
            results.append(result)
            print(f"{result.verdict:<16} {name:<28} {url}")
    finally:
        http.close()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="収集候補の robots.txt を一括調査")
    parser.add_argument("--file", type=Path, help="エリア/サイト名/URL のTSV")
    parser.add_argument("url", nargs="*", help="個別に指定する場合")
    parser.add_argument("--csv", type=Path, help="結果の書き出し先")
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.app.log_dir, config.app.log_level)

    sites: list[tuple[str, str, str]] = []
    if args.file:
        sites += load_candidates(args.file)
    sites += [("", domain_of(u), u) for u in args.url]
    if not sites:
        print("調査対象がありません（--file か URL を指定してください）", file=sys.stderr)
        return 2

    interval = config.http.request_interval_sec
    print(f"{len(sites)} 件を {interval:g} 秒間隔で順に調べます。")
    print("robots.txt とトップページ以外は取得しません。\n")

    results = survey(config, sites)

    if args.csv:
        with args.csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(HEADER)
            writer.writerows(r.row() for r in results)
        print(f"\n書き出しました: {args.csv}")

    ok = sum(1 for r in results if r.verdict.startswith("○"))
    print(f"\nrobots の層で通ったもの: {ok} / {len(results)}")
    print(
        "※ これは robots.txt だけの判定です。**利用規約と掲載画像の権利は別問題**で、\n"
        "   仲介・ポータル系は robots が許していても規約で自動収集を禁じ、写真の\n"
        "   再配布も認めていないのが通例です。○ が出ても人が規約を読んで判断すること。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
