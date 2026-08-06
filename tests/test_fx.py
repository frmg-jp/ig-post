"""円換算レートの取得と保管。

ネットワークは呼ばず、提供元の応答を差し替える。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.fx import (
    FxError,
    effective_rates,
    fetch_rates,
    load_rates,
    save_rates,
    update_rates,
)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    return connect(path)


@pytest.fixture()
def config():
    return load_config("config.yaml")


# 提供元は base=JPY で「1円あたりの各通貨」を返す。
LIVE = {"rates": {"USD": 0.006345, "EUR": 0.005493, "TWD": 0.20491, "JPY": 1.0}}


def _fake_get(_url):
    return LIVE


def test_rates_are_inverted_into_yen_per_unit() -> None:
    """提供元の値をそのまま使うと桁が逆になる。"""
    rates = fetch_rates(["USD", "TWD", "JPY"], http_get=_fake_get)

    assert rates["JPY"] == 1.0
    assert rates["USD"] == pytest.approx(157.6, abs=0.1)   # 1 / 0.006345
    assert rates["TWD"] == pytest.approx(4.88, abs=0.01)   # 1 / 0.20491


def test_currency_the_provider_does_not_have_is_skipped(db) -> None:
    """1通貨欠けても、残りで並べ替えは動く。"""
    rates = fetch_rates(["USD", "XYZ"], http_get=_fake_get)
    assert "USD" in rates
    assert "XYZ" not in rates


def test_zero_or_negative_rates_are_refused() -> None:
    """0で割ると壊れる。値が壊れていたらその通貨だけ落とす。"""
    broken = {"rates": {"USD": 0, "EUR": -1, "TWD": 0.2}}
    rates = fetch_rates(["USD", "EUR", "TWD"], http_get=lambda _u: broken)
    assert list(rates) == ["TWD"]


def test_empty_response_is_an_error() -> None:
    with pytest.raises(FxError):
        fetch_rates(["USD"], http_get=lambda _u: {"rates": {}})


def test_save_and_load_round_trip(db) -> None:
    save_rates(db, {"USD": 157.6, "JPY": 1.0})
    rates, fetched_at = load_rates(db)

    assert rates == {"USD": 157.6, "JPY": 1.0}
    assert fetched_at


def test_saving_again_replaces_rather_than_duplicates(db) -> None:
    save_rates(db, {"USD": 150.0})
    save_rates(db, {"USD": 157.6})

    rates, _ = load_rates(db)
    assert rates == {"USD": 157.6}


def test_effective_rates_fall_back_to_config(db, config) -> None:
    """一度も取得していない環境でも並べ替えが動くこと。"""
    rates, as_of = effective_rates(db, config)

    assert rates == config.fx.jpy_per
    assert as_of == config.fx.as_of


def test_stored_rates_beat_config(db, config) -> None:
    save_rates(db, {"USD": 999.0})
    rates, as_of = effective_rates(db, config)

    assert rates == {"USD": 999.0}
    assert as_of != config.fx.as_of


def test_update_fetches_when_there_is_nothing_stored(db, config) -> None:
    assert update_rates(db, config, http_get=_fake_get) == "updated"
    rates, _ = load_rates(db)
    assert rates["USD"] == pytest.approx(157.6, abs=0.1)


def test_update_does_nothing_while_the_rates_are_fresh(db, config) -> None:
    """定期実行は毎日呼ぶ。実際に外へ出るのは7日を過ぎたときだけ。"""
    now = datetime.now(UTC)
    save_rates(db, {"USD": 150.0}, now=now)

    calls = []

    def counting_get(url):
        calls.append(url)
        return LIVE

    assert update_rates(db, config, http_get=counting_get, now=now + timedelta(days=6)) == "fresh"
    assert calls == []


def test_update_fetches_once_a_week(db, config) -> None:
    now = datetime.now(UTC)
    save_rates(db, {"USD": 150.0}, now=now)

    outcome = update_rates(db, config, http_get=_fake_get, now=now + timedelta(days=7, minutes=1))
    assert outcome == "updated"
    rates, _ = load_rates(db)
    assert rates["USD"] != 150.0


def test_force_ignores_the_age(db, config) -> None:
    now = datetime.now(UTC)
    save_rates(db, {"USD": 150.0}, now=now)

    assert update_rates(db, config, force=True, http_get=_fake_get, now=now) == "updated"


def test_unreadable_timestamp_triggers_a_refetch(db, config) -> None:
    save_rates(db, {"USD": 150.0})
    db.execute("UPDATE fx_rates SET fetched_at = 'いつか'")
    db.commit()

    assert update_rates(db, config, http_get=_fake_get) == "updated"
