"""設定の読み込みとガードレールの検証。"""

from __future__ import annotations

import copy

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
    with pytest.raises(ValidationError, match="3.0 秒以上"):
        Config.model_validate(data)


def test_parallel_access_to_same_domain_is_rejected(raw: dict) -> None:
    data = copy.deepcopy(raw)
    data["http"]["max_concurrency_per_domain"] = 4
    with pytest.raises(ValidationError, match="並列アクセスは禁止"):
        Config.model_validate(data)


def test_robots_txt_cannot_be_disabled(raw: dict) -> None:
    data = copy.deepcopy(raw)
    data["http"]["respect_robots_txt"] = False
    with pytest.raises(ValidationError, match="robots.txt"):
        Config.model_validate(data)


def test_user_agent_requires_contact(raw: dict) -> None:
    data = copy.deepcopy(raw)
    data["http"]["user_agent"] = "SomeBot/1.0"
    with pytest.raises(ValidationError, match="連絡先"):
        Config.model_validate(data)


def test_scoring_weights_must_sum_to_one(raw: dict) -> None:
    data = copy.deepcopy(raw)
    data["scoring"]["weights"]["story"] = 0.9
    with pytest.raises(ValidationError, match="合計は 1.0"):
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


def test_source_rank_lookup() -> None:
    cfg = load_config(CONFIG_PATH)
    assert cfg.source_rank("dezeen") == "S"
    assert cfg.source_rank("archdaily") == "A"
    assert cfg.source_rank("zillow") == "B"
    assert cfg.source_rank("unknown_source") is None
