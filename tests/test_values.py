"""価格・築年の数値化。

原文は表示のために残し、並べ替えと足切りはここで作った数値を使う。
実際に収集で出た書式を並べてある。
"""

from __future__ import annotations

import pytest

from freming.values import parse_price, parse_year


@pytest.mark.parametrize(
    ("text", "value", "currency"),
    [
        ("$1,250,000", 1_250_000, "USD"),
        ("US$1,250,000", 1_250_000, "USD"),
        ("€850,000", 850_000, "EUR"),
        ("£1.2m", 1_200_000, "GBP"),
        ("$3.4M", 3_400_000, "USD"),
        ("¥50,000,000", 50_000_000, "JPY"),
        ("NT$12,000,000", 12_000_000, "TWD"),
        # 台湾の表記。萬=1万、億=1億。記号が無くても単位で通貨が分かる。
        ("3,980 萬", 39_800_000, "TWD"),
        ("1,488萬", 14_880_000, "TWD"),
        ("1.2億", 120_000_000, "TWD"),
    ],
)
def test_parse_price(text, value, currency) -> None:
    assert parse_price(text) == (float(value), currency)


@pytest.mark.parametrize("text", ["Price on request", "", None, "お問い合わせください"])
def test_price_that_cannot_be_read(text) -> None:
    assert parse_price(text) == (None, None)


def test_price_units_only_count_right_after_the_number() -> None:
    """文中の別の場所にある単位を拾わない。"""
    assert parse_price("$500,000 — 万一の際は")[0] == 500_000


def test_price_sorts_numerically_not_as_text() -> None:
    """文字列順では "$9,000" が "$10,000" の後ろに来てしまう。"""
    texts = ["$1,250,000", "$9,000", "$10,000"]
    assert sorted(texts, key=lambda t: parse_price(t)[0]) == ["$9,000", "$10,000", "$1,250,000"]


@pytest.mark.parametrize(
    ("text", "year"),
    [
        ("1868", 1868),
        ("built in 1902", 1902),
        ("c. 1920s", 1920),        # 年代表記。末尾の s で単語境界が切れる
        ("1890–1900", 1890),       # 範囲は古いほうを採る
        ("2015", 2015),
        ("Built 1999, renovated 2018", 1999),
    ],
)
def test_parse_year(text, year) -> None:
    assert parse_year(text) == year


@pytest.mark.parametrize("text", ["Unit 4501", "90210", "1,250,000", "", None, "築年不明"])
def test_year_that_should_not_be_read(text) -> None:
    """部屋番号・郵便番号・価格を築年と取り違えない。"""
    assert parse_year(text) is None
