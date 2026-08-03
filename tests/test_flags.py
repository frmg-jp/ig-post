"""国名から国旗の絵文字を作るテスト。

**間違った国旗を出さないこと**が一番大事。所在地は審査の判断材料なので、
誤りは害になる。分からない国には何も出さない。
"""

from __future__ import annotations

from freming.web.flags import alpha2, flag


def test_focus_area_countries_have_flags() -> None:
    assert flag("United States") == "🇺🇸"
    assert flag("Portugal") == "🇵🇹"
    assert flag("Spain") == "🇪🇸"
    assert flag("Taiwan") == "🇹🇼"


def test_countries_seen_in_actual_candidates() -> None:
    """実際に候補として上がってきた国。"""
    assert flag("France") == "🇫🇷"
    assert flag("Mexico") == "🇲🇽"
    assert flag("United Kingdom") == "🇬🇧"


def test_unknown_countries_get_no_flag() -> None:
    """当てずっぽうで旗を出さない。"""
    assert flag("Wakanda") == ""
    assert flag("") == ""
    assert flag(None) == ""


def test_common_variants_are_accepted() -> None:
    """LLM と記事本文で表記が揺れる。"""
    for name in ("USA", "usa", "United States of America", " United States. "):
        assert flag(name) == "🇺🇸"
    for name in ("UK", "England", "Great Britain"):
        assert flag(name) == "🇬🇧"


def test_alpha2_is_two_uppercase_letters() -> None:
    assert alpha2("Japan") == "JP"
    assert alpha2("Wakanda") is None


def test_flag_is_built_from_regional_indicators() -> None:
    """絵文字を直接並べず計算で作る。国を足すのは対応表の1行で済む。"""
    assert flag("Japan") == "\U0001F1EF\U0001F1F5"
