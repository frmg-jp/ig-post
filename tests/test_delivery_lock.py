"""納品を1プロセスに限るロック。

二重納品は取り返しがつかない。同じ物件に2つのフォルダができ、
frmg_igNNN の連番も飛ぶ。launchd の定期実行と手叩きの deliver、
あるいは serve のワーカーが重なる経路が実際にあるので、
「重なったら片方が退く」ことを固定しておく。
"""

from __future__ import annotations

import pytest

from freming.config import load_config
from freming.delivery.lock import DeliveryInProgress, delivery_lock, lock_path


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.images.work_dir = tmp_path / "images"
    return cfg


def test_the_lock_is_taken_and_released(config) -> None:
    with delivery_lock(config):
        pass
    # 解放後はもう一度取れる。取りっぱなしにすると次の定期実行が全部空振りする。
    with delivery_lock(config):
        pass


def test_a_second_delivery_backs_off(config) -> None:
    with delivery_lock(config):
        with pytest.raises(DeliveryInProgress):
            with delivery_lock(config):
                pass


def test_the_lock_is_released_when_delivery_fails(config) -> None:
    """途中で落ちてもロックを残さない。残すと以後ずっと納品できなくなる。"""
    with pytest.raises(ValueError):
        with delivery_lock(config):
            raise ValueError("納品の途中で失敗")
    with delivery_lock(config):
        pass


def test_the_holder_is_recorded(config) -> None:
    """誰が持っているか分かるようにしておく（詰まったときの調査用）。"""
    import os

    with delivery_lock(config):
        assert lock_path(config).read_text().strip() == str(os.getpid())


def test_the_lock_sits_next_to_the_work_dir(config) -> None:
    assert lock_path(config).parent == config.images.work_dir.parent
