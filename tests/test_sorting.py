"""審査UIの並べ替えと、数値列の作られ方。

価格・築年は原文（TEXT・通貨混在）のまま保存しているので、そのままでは
並べ替えられない。保存時に数値列を作り、順序はそちらで決める。
"""

from __future__ import annotations

import pytest

from freming.collect.base import Candidate
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import (
    SORTS,
    backfill_values,
    insert_candidate,
    list_properties,
    refresh_values,
)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    return connect(path)


def _add(conn, title: str, price=None, year=None) -> int:
    property_id = insert_candidate(
        conn,
        Candidate(
            source="dezeen", source_rank="S",
            source_url=f"https://x.example.com/{title}", title=title,
            content_text="x", for_sale_evidence="y", signal_score=None,
            price=price, location_city=None, location_country=None,
            is_for_sale=1, thumbnail_url="https://t/x.jpg",
        ),
    )
    if year is not None:
        conn.execute("UPDATE properties SET year_built = ? WHERE id = ?", (year, property_id))
        refresh_values(conn, property_id)
    conn.commit()
    return property_id


def _titles(conn, sort):
    return [r["title"] for r in list_properties(conn, status="pending", sort=sort, limit=50)]


def test_numeric_columns_are_written_on_insert(db) -> None:
    _add(db, "A", price="$1,250,000")
    row = db.execute("SELECT price_value, price_currency FROM properties").fetchone()
    assert row["price_value"] == 1_250_000
    assert row["price_currency"] == "USD"


def test_year_is_written_when_scoring_fills_it_in(db) -> None:
    """築年は採点で入る。収集時点では分からない。"""
    _add(db, "A", year="built in 1902")
    row = db.execute("SELECT year_built_value FROM properties").fetchone()
    assert row["year_built_value"] == 1902


def test_price_sort_is_numeric(db) -> None:
    _add(db, "high", price="$1,250,000")
    _add(db, "low", price="$9,000")
    _add(db, "mid", price="$10,000")

    assert _titles(db, "price_asc") == ["low", "mid", "high"]
    assert _titles(db, "price_desc") == ["high", "mid", "low"]


def test_rows_without_a_price_go_last_in_both_directions(db) -> None:
    """値の無い行が先頭に固まると、順序を変えるたびに同じ行を読まされる。"""
    _add(db, "priced", price="$100,000")
    _add(db, "unknown", price=None)

    assert _titles(db, "price_asc")[-1] == "unknown"
    assert _titles(db, "price_desc")[-1] == "unknown"


def test_built_oldest_puts_the_oldest_first(db) -> None:
    _add(db, "new", year="2015")
    _add(db, "old", year="1868")
    _add(db, "mid", year="1955")
    _add(db, "unknown")

    assert _titles(db, "built_oldest") == ["old", "mid", "new", "unknown"]


def test_newest_and_oldest_are_opposites(db) -> None:
    for title in ("first", "second", "third"):
        _add(db, title)
    assert _titles(db, "newest") == list(reversed(_titles(db, "oldest")))


def test_unknown_sort_falls_back_instead_of_reaching_sql(db) -> None:
    """SORTS の値は SQL に直接埋まる。未知のキーを通さないこと。"""
    _add(db, "A", price="$1")
    injected = "'; DROP TABLE properties; --"
    assert _titles(db, injected) == _titles(db, "score")
    assert db.execute("SELECT COUNT(*) AS n FROM properties").fetchone()["n"] == 1


def test_every_sort_key_runs(db) -> None:
    _add(db, "A", price="$1", year="1900")
    for key in SORTS:
        assert _titles(db, key) == ["A"]


def test_backfill_fills_rows_written_before_the_columns_existed(db) -> None:
    _add(db, "A", price="$250,000", year="1899")
    # 0008 より前に入った行を模す
    db.execute("UPDATE properties SET price_value = NULL, year_built_value = NULL")
    db.commit()

    assert backfill_values(db) == 1
    row = db.execute("SELECT price_value, year_built_value FROM properties").fetchone()
    assert row["price_value"] == 250_000
    assert row["year_built_value"] == 1899


# ----------------------------------------------------------------------
# 円換算での価格の並べ替え。
#
# 価格は原文の通貨のまま保存・表示している。通貨をまたいで「高い順・安い順」を
# 出すには換算が要るが、DBには換算値を保存していない（レートを直すたびに
# 全件を書き直すことになるため）。並べ替えのたびに計算する。

RATES = {"JPY": 1.0, "USD": 150.0, "GBP": 195.0, "TWD": 4.7, "EUR": 165.0}


def test_price_sort_without_rates_compares_raw_numbers(db) -> None:
    """レートを渡さないと、通貨を無視した数値の大小になる。

    3,980萬(TWD) = 39,800,000 が $1,250,000 より大きい数として先に来る。
    これは金額として比べたことにならない、というのが換算を入れた理由。
    """
    _add(db, "usd", price="$1,250,000")
    _add(db, "twd", price="3,980 萬")

    assert _titles(db, "price_desc") == ["twd", "usd"]


def test_price_sort_with_rates_compares_in_yen(db) -> None:
    """換算すると USD 1.25M（約1.9億円）が TWD 3,980萬（約1.87億円）の上に来る。"""
    _add(db, "usd", price="$1,250,000")
    _add(db, "twd", price="3,980 萬")

    got = [
        r["title"]
        for r in list_properties(db, status="pending", sort="price_desc", fx_rates=RATES)
    ]
    assert got == ["usd", "twd"]


def test_yen_conversion_orders_four_currencies(db) -> None:
    _add(db, "gbp", price="£1.2m")       # 234,000,000 円
    _add(db, "usd", price="$1,250,000")  # 187,500,000 円
    _add(db, "eur", price="€850,000")    # 140,250,000 円
    _add(db, "cheap", price="$9,000")    #   1,350,000 円

    got = [
        r["title"]
        for r in list_properties(db, status="pending", sort="price_desc", fx_rates=RATES)
    ]
    assert got == ["gbp", "usd", "eur", "cheap"]


def test_currency_without_a_rate_goes_last(db) -> None:
    """換算できないものを金額だけで混ぜない。桁が違うと順序が壊れる。"""
    _add(db, "usd", price="$100,000")
    _add(db, "unknown_currency", price="100,000")   # 通貨記号が無い

    for sort in ("price_desc", "price_asc"):
        got = [
            r["title"]
            for r in list_properties(db, status="pending", sort=sort, fx_rates=RATES)
        ]
        assert got[-1] == "unknown_currency", sort


def test_rates_are_not_stored_in_the_database(db) -> None:
    """レートを変えたら、backfill せずに順序が変わること。"""
    _add(db, "usd", price="$1,000,000")
    _add(db, "gbp", price="£900,000")

    cheap_pound = {"USD": 150.0, "GBP": 100.0}   # USD 1.5億 / GBP 0.9億
    rich_pound = {"USD": 150.0, "GBP": 250.0}    # USD 1.5億 / GBP 2.25億

    def top(rates):
        return list_properties(db, status="pending", sort="price_desc", fx_rates=rates)[0]["title"]

    assert top(cheap_pound) == "usd"
    assert top(rich_pound) == "gbp"


def test_price_expression_ignores_non_alphabetic_currency_keys(db) -> None:
    """config に妙なキーが混ざってもSQLを壊さない。"""
    from freming.db.repository import price_jpy_expr

    expr = price_jpy_expr({"USD": 150.0, "'; DROP TABLE properties; --": 1.0})
    assert "DROP TABLE" not in expr
    _add(db, "usd", price="$1")
    got = list_properties(
        db, status="pending", sort="price_desc",
        fx_rates={"USD": 150.0, "'; DROP TABLE properties; --": 1.0},
    )
    assert [r["title"] for r in got] == ["usd"]
