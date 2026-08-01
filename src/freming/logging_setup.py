"""ログ設定。全処理を logs/freming-YYYY-MM-DD.log に出力する。

方針: 例外は握りつぶさない。捕捉した例外は必ずスタックトレース付きで
ファイルに残す（exc_info=True / logger.exception を使う）。
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_configured = False


def setup_logging(log_dir: str | Path = "logs", level: str = "INFO") -> Path:
    """コンソールと日付別ログファイルの両方にハンドラを設定する。

    Returns:
        書き出し先のログファイルパス。
    """
    global _configured
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"freming-{date.today().isoformat()}.log"

    root = logging.getLogger()
    if _configured:
        return log_file

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 未捕捉例外もログファイルに残す
    def _excepthook(exc_type, exc_value, exc_tb):  # pragma: no cover - 異常系
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        root.critical("未捕捉の例外", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _excepthook

    # 依存ライブラリの冗長ログを抑制
    for noisy in ("httpx", "httpcore", "googleapiclient.discovery_cache", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    return log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
