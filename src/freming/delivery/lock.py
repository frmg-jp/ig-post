"""納品を1プロセスに限るためのロック。

納品は途中で止まると取り返しがつかない。「納品済みか」の確認から Drive への
書き込みまでには隙間があり、フォルダ名も「既存の最大値＋1」で決めているので、
2つのプロセスが同時に走ると同じ `frmg_igNNN` を取り合って二重納品になる。

同じ Mac の中で起きうる組み合わせ:

  - launchd の定期実行と、手で叩いた `deliver` が重なる
  - `serve`（中でワーカーが動く）を開いたまま、定期実行が起きる

ファイルロックなので**同じマシンの中だけ**を守る。別マシンとの重複は
そもそもワーカーを1箇所に寄せることで避ける（docs/review-ui-hosting.md）。
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from freming.config import Config
from freming.logging_setup import get_logger

log = get_logger(__name__)


class DeliveryInProgress(RuntimeError):
    """別のプロセスが納品中。待たずに諦める。

    待って順番に流すこともできるが、定期実行が積み上がると
    「15分おきのはずが何本も溜まっている」状態になる。次の回に回す方がよい。
    """


def lock_path(config: Config) -> Path:
    return Path(config.images.work_dir).parent / "delivery.lock"


@contextmanager
def delivery_lock(config: Config) -> Iterator[None]:
    """納品の実行権を取る。取れなければ DeliveryInProgress。"""
    path = lock_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    # ロックの保持者が分かるように PID を書く。落ちた場合でも
    # flock はプロセス終了で解放されるので、消し忘れは残らない。
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DeliveryInProgress(
                f"別のプロセスが納品中です（{path}）。今回は何もしません"
            ) from None
        os.ftruncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


__all__ = ["DeliveryInProgress", "delivery_lock", "lock_path"]
