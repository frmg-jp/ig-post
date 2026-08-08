"""設定の読み込みとガードレールの検証。"""

from __future__ import annotations

import copy
import re

import pytest
import yaml
from pydantic import ValidationError

from freming.config import Config, load_config

CONFIG_PATH = "config.yaml"


@pytest.fixture()
def raw() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_repository_config_is_valid() -> None:
    cfg = load_config(CONFIG_PATH)
    assert cfg.drive.parent_folder_id
    assert cfg.scoring.weights.total == pytest.approx(1.0)


def test_request_interval_must_be_at_least_3_seconds(raw: dict) -> None:
    data = copy.deepcopy(raw)
    data["http"]["request_interval_sec"] = 1.0
    with pytest.raises(ValidationError, match=re.escape("3.0 秒以上")):
        Config.model_validate(data)


def test_parallel_access_to_same_domain_is_rejected(raw: dict) -> None:
    data = copy.deepcopy(raw)
    data["http"]["max_concurrency_per_domain"] = 4
    with pytest.raises(ValidationError, match="並列アクセスは禁止"):
        Config.model_validate(data)


def test_robots_txt_cannot_be_disabled(raw: dict) -> None:
    data = copy.deepcopy(raw)
    data["http"]["respect_robots_txt"] = False
    with pytest.raises(ValidationError, match=re.escape("robots.txt")):
        Config.model_validate(data)


def test_user_agent_requires_contact(raw: dict) -> None:
    data = copy.deepcopy(raw)
    data["http"]["user_agent"] = "SomeBot/1.0"
    with pytest.raises(ValidationError, match="連絡先"):
        Config.model_validate(data)


def test_scoring_weights_must_sum_to_one(raw: dict) -> None:
    data = copy.deepcopy(raw)
    data["scoring"]["weights"]["story"] = 0.9
    with pytest.raises(ValidationError, match=re.escape("合計は 1.0")):
        Config.model_validate(data)


def test_manual_only_sources_are_never_crawled() -> None:
    """Zillow / Redfin / Compass は自動収集の対象に入らないこと。"""
    cfg = load_config(CONFIG_PATH)
    crawlable = {s.key for s in cfg.crawlable_listing_sources()}
    for key in ("zillow", "redfin", "compass"):
        source = cfg.listing_source(key)
        assert source is not None, f"{key} が config に定義されていない"
        assert source.mode == "manual_only"
        assert key not in crawlable


def test_approval_criteria_are_available_to_scoring() -> None:
    """承認実例と判断軸がプロンプトに渡せる状態で設定されていること。

    docs/approval-criteria.md の内容を [2] スコアリングが参照する。
    片方だけ空になっていると基準が伝わらないため両方を必須にする。
    """
    cfg = load_config(CONFIG_PATH)
    assert cfg.scoring.approved_examples, "scoring.approved_examples が空"
    assert cfg.scoring.approval_notes, "scoring.approval_notes が空"


def test_priority_genres_all_have_keywords() -> None:
    """priority に挙げたジャンルには必ず判定用キーワードがあること。"""
    cfg = load_config(CONFIG_PATH)
    for genre in cfg.genres.priority:
        assert cfg.genres.keywords.get(genre), f"{genre} のキーワードが未定義"


def test_source_rank_lookup() -> None:
    cfg = load_config(CONFIG_PATH)
    assert cfg.source_rank("dezeen") == "S"
    assert cfg.source_rank("archdaily") == "A"
    assert cfg.source_rank("zillow") == "B"
    assert cfg.source_rank("unknown_source") is None


def test_delivery_polling_cannot_be_too_frequent() -> None:
    """総当たりで承認済みを探しに行かないための下限。"""
    from freming.config import DeliveryConfig

    with pytest.raises(ValidationError, match=re.escape("5.0 秒以上")):
        DeliveryConfig(poll_interval_sec=1)


def test_delivery_must_allow_at_least_one_attempt() -> None:
    from freming.config import DeliveryConfig

    with pytest.raises(ValidationError, match="1 以上"):
        DeliveryConfig(max_attempts=0)


def test_delivery_defaults_are_on() -> None:
    """承認したらそのまま納品まで進むのが既定。"""
    cfg = load_config("config.yaml")
    assert cfg.delivery.auto is True


def test_robb_report_excludes_art_auctions_and_listicles() -> None:
    """物件以外のセクションを、記事を取りに行く前に落とす。

    /art-collectibles/ は美術品オークション（金額は出るが物件ではない）、
    /lists/ は「所有物件まとめ」のようなリスト記事で、納品する素材にならない。
    """
    cfg = load_config("config.yaml")
    source = cfg.editorial_source("robbreport_shelter")
    assert source is not None and source.enabled

    base = "https://robbreport.com/shelter"
    assert not source.url_allowed(f"{base}/art-collectibles/monet-auction-123")
    assert not source.url_allowed(f"{base}/celebrity-homes/lists/tom-cruise-portfolio-123")
    assert source.url_allowed(f"{base}/homes-for-sale/cotswolds-barn-for-sale-123")
    assert source.url_allowed(f"{base}/celebrity-homes/betsey-johnson-house-123")


def test_robb_report_skips_its_composite_lead_image() -> None:
    """セレブ記事の代表画像に人物の顔写真が丸く重ねてある。"""
    cfg = load_config("config.yaml")
    assert cfg.editorial_source("robbreport_shelter").skip_lead_image is True
    # 他のソースは既定のまま（先頭を使う）
    assert cfg.editorial_source("thespaces").skip_lead_image is False


# --- Drive の認証方式の上書き ------------------------------------------
#
# 納品を GitHub Actions（Workload Identity 連携）へ移す途中、手元の
# oauth と両立させる必要がある。config.yaml を書き換えると、
# どちらか片方が必ず壊れる。


def test_環境変数で認証方式を上書きできる(monkeypatch) -> None:
    from freming.config import load_config

    monkeypatch.setenv("FREMING_DRIVE_AUTH_MODE", "adc")
    assert load_config("config.yaml").drive.auth_mode == "adc"


def test_未設定なら_configのまま(monkeypatch) -> None:
    from freming.config import load_config

    monkeypatch.delenv("FREMING_DRIVE_AUTH_MODE", raising=False)
    assert load_config("config.yaml").drive.auth_mode == "oauth"


def test_知らない方式は起動時に落ちる(monkeypatch) -> None:
    import pytest as _pytest

    from freming.config import load_config

    monkeypatch.setenv("FREMING_DRIVE_AUTH_MODE", "てきとう")
    with _pytest.raises(ValueError, match="FREMING_DRIVE_AUTH_MODE"):
        load_config("config.yaml")
