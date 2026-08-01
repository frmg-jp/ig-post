#!/usr/bin/env python3
"""Drive 疎通確認スクリプト（最優先。ここが通らないと他が無意味）。

実行:
    python scripts/check_drive.py
    python scripts/check_drive.py --no-cleanup   # テスト用ファイルを残す

終了コード:
    0 = 疎通OK / 1 = 疎通NG / 2 = 設定エラー
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# src レイアウトのままでも実行できるようにする
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freming.config import load_config  # noqa: E402
from freming.delivery.preflight import format_report, run_preflight  # noqa: E402
from freming.logging_setup import get_logger, setup_logging  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Google Drive への疎通・書き込み権限を検証する")
    parser.add_argument("--config", default=None, help="config.yaml のパス")
    parser.add_argument(
        "--no-cleanup", action="store_true", help="作成したテスト用ファイルを削除しない"
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="ブラウザを自動で開かず、認証URLを表示するだけにする",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - 設定エラーは分けて報告する
        print(f"設定の読み込みに失敗しました: {exc}", file=sys.stderr)
        return 2

    log_file = setup_logging(cfg.app.log_dir, cfg.app.log_level)
    log = get_logger("check_drive")
    log.info("Drive 疎通確認を開始します（ログ: %s）", log_file)

    if not cfg.drive.enabled:
        print("drive.enabled が false のため確認をスキップしました。", file=sys.stderr)
        return 2

    report = run_preflight(
        cfg.drive, cleanup=not args.no_cleanup, open_browser=not args.no_browser
    )
    print(format_report(report))
    log.info("Drive 疎通確認の結果: %s", "OK" if report.ok else "NG")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
