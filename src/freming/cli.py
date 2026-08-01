"""CLI 入口。各モジュールは単体でも実行できるが、ここから一括で呼べる。

    freming check-drive            # Drive 疎通確認（最優先）
    freming db migrate             # マイグレーション適用
    freming db status              # 適用状況

（collect / score / serve / deliver は各フェーズの実装時に追加する）
"""

from __future__ import annotations

import argparse
import sys

from freming.config import load_config
from freming.logging_setup import setup_logging


def _cmd_check_drive(args: argparse.Namespace) -> int:
    from freming.delivery.preflight import format_report, run_preflight

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    if not cfg.drive.enabled:
        print("drive.enabled が false です。", file=sys.stderr)
        return 2
    report = run_preflight(
        cfg.drive, cleanup=not args.no_cleanup, open_browser=not args.no_browser
    )
    print(format_report(report))
    return 0 if report.ok else 1


def _cmd_db(args: argparse.Namespace) -> int:
    from freming.db import migrate as migrate_mod

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    db_path = args.db or cfg.app.db_path
    if args.db_action == "status":
        for version, applied in migrate_mod.status(db_path):
            print(f"{'[x]' if applied else '[ ]'} {version}")
        return 0
    migrate_mod.migrate(db_path)
    print(f"OK: {db_path}")
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    from freming.collect.editorial import collect_source

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    stats = collect_source(cfg, args.source, args.limit, args.dry_run, args.explain)
    print(stats.summary())
    for url in stats.candidates:
        print(f"  - {url}")
    if args.explain:
        print(stats.explain_report())
    return 0


def _cmd_probe_feed(args: argparse.Namespace) -> int:
    """候補のフィードURLをまとめて試し、使えるものを見つける。"""
    from freming.collect.editorial import probe_feed

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    results: list[tuple[str, str]] = []
    for url in args.url:
        try:
            stats = probe_feed(cfg, url, args.limit)
        except Exception as exc:  # noqa: BLE001 - 1つの失敗で残りを止めない
            results.append((url, f"NG   {type(exc).__name__}: {str(exc)[:60]}"))
            continue

        if stats.feed_entries == 0:
            results.append((url, "NG   フィードとして読めない/記事0件"))
            continue

        chars = sorted(e.text_chars for e in stats.explanations) or [0]
        median = chars[len(chars) // 2]
        results.append(
            (
                url,
                f"OK   {stats.feed_entries:>3}件  本文中央値 {median:>5}字  "
                f"候補 {stats.inserted}件",
            )
        )
        if args.details and stats.explanations:
            print(f"\n### {url}")
            print(stats.explain_report(top=args.top))

    print("\n=== フィード調査結果 ===")
    for url, line in results:
        print(f"{line}   {url}")
    return 0


def _cmd_ingest_url(args: argparse.Namespace) -> int:
    from freming.collect.manual import AlreadyCollected, ingest_url
    from freming.net.client import RobotsDisallowed

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    try:
        property_id = ingest_url(cfg, args.url)
    except (AlreadyCollected, RobotsDisallowed) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OK: property_id={property_id}")
    return 0


def _cmd_add_manual(args: argparse.Namespace) -> int:
    from freming.collect.manual import AlreadyCollected, add_manual_entry

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    try:
        property_id = add_manual_entry(
            cfg,
            source_url=args.url,
            title=args.title,
            price=args.price,
            city=args.city,
            country=args.country,
            note=args.note,
            thumbnail_url=args.image,
        )
    except AlreadyCollected as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OK: property_id={property_id}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from freming.db.connection import connect
    from freming.db.repository import count_by_status

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    conn = connect(cfg.app.db_path)
    try:
        counts = count_by_status(conn)
    finally:
        conn.close()
    if not counts:
        print("候補はまだありません。")
        return 0
    for status in ("pending", "approved", "rejected", "delivered"):
        print(f"  {status:<10} {counts.get(status, 0)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="freming", description="FREMING CURATED パイプライン")
    parser.add_argument("--config", default=None, help="config.yaml のパス")
    sub = parser.add_subparsers(dest="command", required=True)

    p_drive = sub.add_parser("check-drive", help="Drive 疎通確認")
    p_drive.add_argument("--no-cleanup", action="store_true")
    p_drive.add_argument("--no-browser", action="store_true")
    p_drive.set_defaults(func=_cmd_check_drive)

    p_db = sub.add_parser("db", help="DB操作")
    p_db.add_argument("db_action", choices=["migrate", "status"])
    p_db.add_argument("--db", default=None)
    p_db.set_defaults(func=_cmd_db)

    p_collect = sub.add_parser("collect", help="編集ソースから収集（経路B）")
    p_collect.add_argument("--source", required=True, help="editorial_sources の key")
    p_collect.add_argument("--limit", type=int, default=None)
    p_collect.add_argument("--dry-run", action="store_true")
    p_collect.add_argument(
        "--explain", action="store_true", help="閾値未満も含めて判定内訳を表示（調整用）"
    )
    p_collect.set_defaults(func=_cmd_collect)

    p_probe = sub.add_parser(
        "probe-feed", help="任意のフィードURLを試して判定内訳だけ表示（DBに書き込まない）"
    )
    p_probe.add_argument("url", nargs="+", help="調べるフィードURL（複数可）")
    p_probe.add_argument("--limit", type=int, default=None)
    p_probe.add_argument("--top", type=int, default=15)
    p_probe.add_argument("--details", action="store_true", help="各フィードの判定内訳も表示")
    p_probe.set_defaults(func=_cmd_probe_feed)

    p_ingest = sub.add_parser("ingest-url", help="URLを1件だけ取得して候補化")
    p_ingest.add_argument("url")
    p_ingest.set_defaults(func=_cmd_ingest_url)

    p_manual = sub.add_parser(
        "add-manual",
        help="ページを取得せず手入力で候補化（自動収集が禁止されているサイト用）",
    )
    p_manual.add_argument("--url", required=True, help="物件ページのURL（取得はしない）")
    p_manual.add_argument("--title", required=True)
    p_manual.add_argument("--price", default=None, help="原文のまま（例: $2,400,000）")
    p_manual.add_argument("--city", default=None)
    p_manual.add_argument("--country", default=None)
    p_manual.add_argument("--note", default=None, help="スコアリングに渡す補足メモ")
    p_manual.add_argument("--image", default=None, help="サムネイル画像のURL")
    p_manual.set_defaults(func=_cmd_add_manual)

    p_status = sub.add_parser("status", help="候補の件数をステータス別に表示")
    p_status.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
