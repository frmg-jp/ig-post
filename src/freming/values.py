"""原文の価格・築年から、並べ替えと足切りに使える数値を取り出す。

価格は収集元の表記をそのまま保存している（通貨も桁区切りも様々）。表示は
原文が正しいが、順序を決めるには数値が要る。ここで一度だけ数値にして
properties.price_value / price_currency / year_built_value に持たせる。

**為替換算はしない。** 相場の変動する係数を持ち込むと、同じ物件の並び順が
日によって変わる。price_value は原文の通貨のままの数値で、通貨をまたぐ
順序は目安と割り切る（収集対象はほぼ米ドル）。
"""

from __future__ import annotations

import re

# 通貨の見分け。記号が無いものは通貨不明として値だけ取る。
# 「NT$」を「$」より先に見るため、長い記号から順に並べる。
_CURRENCY_MARKS: tuple[tuple[str, str], ...] = (
    ("NT$", "TWD"),
    ("US$", "USD"),
    ("A$", "AUD"),
    ("C$", "CAD"),
    ("HK$", "HKD"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("¥", "JPY"),
    ("￥", "JPY"),
    ("$", "USD"),
    ("USD", "USD"),
    ("EUR", "EUR"),
    ("GBP", "GBP"),
    ("JPY", "JPY"),
    ("TWD", "TWD"),
)

# 台湾の表記。萬 = 1万、億 = 1億。「1,488萬」「1.2億」の形で出る。
_CJK_UNITS: tuple[tuple[str, int], ...] = (("億", 100_000_000), ("萬", 10_000), ("万", 10_000))

# 英語圏の略記。"£1.2m" / "$3.4M" / "1.5k"。
_LATIN_UNITS: tuple[tuple[str, int], ...] = (("m", 1_000_000), ("k", 1_000))

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 建物の築年として妥当な範囲。電話番号や郵便番号を拾わないための枠。
# 末尾の s を許すのは "1920s"（年代表記）のため。\b だけだと s で境界が
# 切れて拾えない。
_YEAR = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})(?=s?\b)")
_YEAR_MIN, _YEAR_MAX = 1000, 2100


def parse_price(text: str | None) -> tuple[float | None, str | None]:
    """原文の価格から (数値, 通貨コード) を返す。読めなければ (None, None)。

    通貨が判別できない場合でも数値は返す。並べ替えの材料としては
    「いくらか分かる」だけで十分に役に立つため。
    """
    if not text:
        return (None, None)
    raw = text.strip()

    currency = None
    for mark, code in _CURRENCY_MARKS:
        if mark in raw:
            currency = code
            break

    match = _NUMBER.search(raw)
    if not match:
        return (None, currency)
    try:
        value = float(match.group(0).replace(",", ""))
    except ValueError:
        return (None, currency)

    # 単位の判定は、数値の直後に続く文字だけを見る。文中の別の場所にある
    # 「万円台の物件も」のような語を拾わないため。
    tail = raw[match.end() :].lstrip()
    for unit, factor in _CJK_UNITS:
        if tail.startswith(unit):
            if currency is None:
                currency = "TWD" if unit in ("億", "萬") else "JPY"
            return (value * factor, currency)
    for unit, factor in _LATIN_UNITS:
        if tail[:1].lower() == unit:
            return (value * factor, currency)

    return (value, currency)


def parse_year(text: str | None) -> int | None:
    """築年の原文から西暦を返す。読めなければ None。

    "1868" のほか "built in 1902"、"c. 1920s"、"1890–1900" のような
    書き方が来る。範囲は先に出るほう（古いほう）を採る。築年として
    妥当な範囲を外れる数値は無視する。
    """
    if not text:
        return None
    for match in _YEAR.finditer(str(text)):
        year = int(match.group(0))
        if _YEAR_MIN <= year <= _YEAR_MAX:
            return year
    return None
