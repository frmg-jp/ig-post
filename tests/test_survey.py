"""収集候補の robots.txt 一括調査のテスト。

このツールが答えるのは robots の層だけ。規約と画像の権利は別問題なので、
「robots が通った＝収集してよい」と読めてしまう出力になっていないかも見る。
"""

from __future__ import annotations

from pathlib import Path
from urllib.robotparser import RobotFileParser

import pytest

from freming.collect.survey import (
    SiteSurvey,
    load_candidates,
    survey_site,
)
from freming.config import load_config
from freming.net.robots import RobotsRules


@pytest.fixture()
def config():
    return load_config("config.yaml")


class _FakeHttp:
    """robots だけ差し替えたクライアント。外には一切出ない。"""

    def __init__(self, body: str | None, fetched: bool = True, missing: bool = False) -> None:
        parser = None
        if body is not None:
            parser = RobotFileParser()
            parser.parse(body.splitlines())
        self._rules = RobotsRules(parser=parser, fetched=fetched, missing=missing)
        self.robots = self

    def rules_for(self, _url: str) -> RobotsRules:
        return self._rules


def test_candidate_file_is_read(tmp_path) -> None:
    path = tmp_path / "sites.tsv"
    path.write_text(
        "# コメント\nエリア\tサイト名\tURL\n"
        "台湾\t591\thttps://www.591.com.tw\n",
        encoding="utf-8",
    )
    assert load_candidates(path) == [("台湾", "591", "https://www.591.com.tw")]


def test_shipped_candidate_list_parses() -> None:
    """リポジトリに置いた候補リストがそのまま読めること。"""
    rows = load_candidates(Path("docs/source-candidates.tsv"))
    assert len(rows) == 61
    assert ("全米", "Zillow", "https://www.zillow.com") in rows


def test_disallow_all_is_reported_as_blocked(config) -> None:
    http = _FakeHttp("User-agent: *\nDisallow: /\n")
    result = survey_site(config, "テスト", "https://example.com", http)
    assert result.top_allowed == "不可"
    assert result.verdict == "×(robots)"


def test_unreadable_robots_is_treated_as_blocked(config) -> None:
    """robots.txt を読めないときは安全側に倒す（取得しない）。"""
    http = _FakeHttp(None, fetched=False)
    result = survey_site(config, "テスト", "https://example.com", http)
    assert result.verdict == "×(robots取得不可)"
    assert "安全側" in result.note


def test_partially_blocked_paths_are_listed(config) -> None:
    """トップは許可でも物件ページ配下だけ禁止、という形を取りこぼさない。"""
    http = _FakeHttp("User-agent: *\nDisallow: /property\nDisallow: /search\n")
    result = survey_site(config, "テスト", "https://example.com", http)
    assert result.top_allowed == "可"
    assert set(result.blocked_paths) == {"/search", "/property"}
    assert result.verdict == "△(一部不可)"


def test_crawl_delay_is_picked_up(config) -> None:
    http = _FakeHttp("User-agent: *\nDisallow:\nCrawl-delay: 20\n")
    result = survey_site(config, "テスト", "https://example.com", http)
    assert result.crawl_delay == 20.0


def test_pass_verdict_says_it_only_covers_robots() -> None:
    """規約まで判定したと読めない文言にしておく。"""
    survey = SiteSurvey("t", "https://example.com", "ok", "可", [], None, [])
    assert survey.verdict == "○(robotsのみ)"


def test_shipped_us_candidate_list_parses() -> None:
    """全米ソースの候補リストがそのまま survey-sources に渡せること。"""
    rows = load_candidates(Path("docs/us-source-candidates.tsv"))
    assert len(rows) == 20
    assert ("全米", "Dwell", "https://www.dwell.com/") in rows
    # 区分の列（4列目）が増えても、エリア/名前/URL の3列として読める
    assert all(url.startswith("http") for _area, _name, url in rows)
