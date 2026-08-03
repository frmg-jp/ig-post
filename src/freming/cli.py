"""CLI 入口。各モジュールは単体でも実行できるが、ここから一括で呼べる。

    freming check-drive            # Drive 疎通確認（最優先）
    freming db migrate             # マイグレーション適用
    freming db status              # 適用状況

（collect / score / serve / deliver は各フェーズの実装時に追加する）
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

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
    db_path = args.db or cfg.app.target()
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
        print(stats.pace_report())
        print(stats.url_pattern_report())
        print(stats.explain_report())
    return 0


def _cmd_sources(args: argparse.Namespace) -> int:
    """編集ソースの key を並べる。

    定期実行のスクリプトから「いま有効なソース」を取るために使う。
    config.yaml を shell 側で解釈させると、設定の持ち方を変えたときに
    ワークフローが黙って壊れる。
    """
    cfg = load_config(args.config)
    for source in cfg.editorial_sources:
        if args.enabled and not source.enabled:
            continue
        if args.verbose:
            state = "有効" if source.enabled else "無効"
            print(f"{source.key:<18} {state}  {source.rank}  {source.name}")
        else:
            print(source.key)
    return 0


def _cmd_probe_feed(args: argparse.Namespace) -> int:
    """候補のフィードURLをまとめて試し、使えるものを見つける。"""
    from freming.collect.editorial import failure_reason, probe_feed

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    # (使えるか, URL, 説明) の3つ組。失敗は原因まで出す。
    # 「読めない」で一括りにすると、URLの誤りなのか robots による拒否なのか、
    # 次に何をすべきかが分からなくなる。
    results: list[tuple[bool, str, str]] = []
    for url in args.url:
        try:
            stats = probe_feed(cfg, url, args.limit)
        except Exception as exc:  # noqa: BLE001 - 1つの失敗で残りを止めない
            results.append((False, url, failure_reason(exc)))
            continue

        if stats.feed_entries == 0:
            reason = stats.feed_failures[0] if stats.feed_failures else "記事0件"
            results.append((False, url, reason))
            continue

        chars = sorted(e.text_chars for e in stats.explanations) or [0]
        median = chars[len(chars) // 2]
        # 「10件」はフィードの窓であって1日分ではない。何日分なのかまで
        # 出さないと、審査に上がる件数の見積もりが桁で外れる。
        # 本/日 は候補が0件でも意味がある（そのソースがどれだけ動いているか）。
        # 週あたりの件数だけを出すと、動いていないのか候補率が低いのか
        # 区別できない。
        rate = stats.entries_per_day
        pace = f"{rate:>5.1f}本/日" if rate is not None else " ペース不明"
        weekly = stats.candidates_per_week
        age = stats.days_since_newest()
        if stats.is_stale():
            # 止まったフィードは「本/日」が健全に見える。ここで言い切る。
            estimate = f"  停止中（最新 {age:.0f} 日前）"
        elif weekly is None:
            estimate = ""
        elif stats.weekly_is_reliable:
            estimate = f"  審査 週{weekly:.1f}件"
        else:
            # 抜粋配信では候補が構造的に0になる。0件と言い切らない。
            estimate = "  審査 要再測定（抜粋配信）"
        results.append(
            (
                True,
                url,
                f"{stats.feed_entries:>3}件  本文中央値 {median:>5}字  "
                f"{pace}  候補 {stats.inserted}件{estimate}",
            )
        )
        if args.details:
            print(f"\n### {url}")
            print(stats.pace_report())
            print(stats.url_pattern_report())
            print(stats.explain_report(top=args.top))

    width = max((len(u) for _, u, _ in results), default=0)
    usable = [r for r in results if r[0]]
    print("\n=== フィード調査結果 ===")
    for ok, url, detail in results:
        print(f"{'OK' if ok else 'NG'}  {url:<{width}}  {detail}")
    print(f"\n読めたフィード {len(usable)} / {len(results)} 件")
    if usable:
        print("候補が出たものを config.yaml の editorial_sources に登録してください。")
    print(
        "\n※ probe は記事ページを取らず、フィードが配信した本文だけで判定します。"
        "抜粋配信の\n"
        "   フィードは候補が構造的に0件になるため、「審査 要再測定」と出したものは"
        "collect で\n"
        "   確かめてください（fetch_article_pages: true で登録してから）。"
    )
    return 0


def _cmd_learn(args: argparse.Namespace) -> int:
    """非承認理由を分類し、頻出する指摘をルール候補にする（[7]）。"""
    from freming.db.connection import session
    from freming.learning.loop import run_learning

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    with session(cfg.app.target()) as conn:
        stats = run_learning(cfg, conn, args.limit)
    print(stats.summary())
    for line in stats.new_candidates:
        print(f"  提案: {line}")
    if stats.new_candidates:
        print("\n採用するなら: python -m freming.cli rules approve <タグ>")
    return 0


def _cmd_rules(args: argparse.Namespace) -> int:
    """ルール候補の確認と承認。自動適用はせず、必ずここを通す。"""
    from freming.db.connection import session
    from freming.db.repository import decide_rule_candidate, list_rule_candidates

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    with session(cfg.app.target()) as conn:
        if args.rules_action == "list":
            rows = list_rule_candidates(conn)
            if not rows:
                print("ルール候補はまだありません。")
                return 0
            for row in rows:
                mark = {"proposed": "[ ]", "approved": "[x]", "dismissed": "[-]"}
                print(
                    f"{mark.get(row['state'], '[?]')} {row['reason_tag']} "
                    f"({row['hit_count']}件)\n    {row['proposal']}"
                )
            return 0

        state = "approved" if args.rules_action == "approve" else "dismissed"
        if not decide_rule_candidate(conn, args.tag, state):
            print(f"'{args.tag}' というルール候補はありません。", file=sys.stderr)
            return 1
    print(f"{args.tag} を {state} にしました。")
    return 0


def _cmd_deliver(args: argparse.Namespace) -> int:
    from freming.db.connection import session
    from freming.delivery.deliver import deliver_approved

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    if not cfg.drive.enabled and not args.dry_run:
        print("drive.enabled が false です。", file=sys.stderr)
        return 2
    if args.watch:
        if args.dry_run:
            print("--watch と --dry-run は同時に使えません。", file=sys.stderr)
            return 2
        return _watch_deliveries(cfg)
    with session(cfg.app.target()) as conn:
        stats = deliver_approved(cfg, conn, args.limit, args.dry_run)
    print(stats.summary())
    print(stats.report())
    return 0


def _watch_deliveries(cfg) -> int:
    """承認済みを拾い続ける常駐モード（審査UIを別マシンで開く場合用）。

    serve を起動していれば同じことが中で動くので、通常は不要。
    """
    import signal

    from freming.delivery.worker import DeliveryWorker

    worker = DeliveryWorker(cfg)
    stop = threading.Event()

    def _handle(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    print(f"承認済みを {cfg.delivery.poll_interval_sec:.0f} 秒ごとに納品します。停止するには Ctrl+C")
    worker.start()
    stop.wait()
    print("停止しています…")
    worker.stop()
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """審査UIを起動する（[3]）。ローカル利用前提で認証は持たない。"""
    import uvicorn

    from freming.web.app import create_app

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    host = args.host or cfg.review_ui.host
    port = args.port or cfg.review_ui.port
    print(f"審査UI:   http://{host}:{port}/")
    print(f"  未審査  http://{host}:{port}/?status=pending")
    print(f"  承認    http://{host}:{port}/?status=approved")
    print(f"  非承認  http://{host}:{port}/?status=rejected")
    print(f"  納品済  http://{host}:{port}/?status=delivered")
    if cfg.delivery.auto and cfg.drive.enabled:
        print("自動納品: ON（承認するとそのまま画像取得→加工→Drive納品まで進みます）")
    else:
        print("自動納品: OFF（承認後に python -m freming.cli deliver を実行してください）")
    print("停止するには Ctrl+C")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level=cfg.app.log_level.lower())
    return 0


def _cmd_check_api(args: argparse.Namespace) -> int:
    """スコアリングAPIの疎通確認。まとめて採点する前に鍵と契約を確かめる。"""
    from freming.scoring.client import check_api

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    ok, message = check_api(cfg)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def _cmd_score(args: argparse.Namespace) -> int:
    from freming.db.connection import session
    from freming.scoring.runner import score_pending

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    with session(cfg.app.target()) as conn:
        stats = score_pending(cfg, conn, args.limit, args.dry_run)
    print(stats.summary())
    print(stats.report())
    return 0


def _cmd_discover_feed(args: argparse.Namespace) -> int:
    """サイトのトップページから公開フィードURLを拾う（推測をやめるため）。"""
    from freming.collect.editorial import discover_feeds, failure_reason

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    total = 0
    found: list[str] = []
    for site in args.url:
        try:
            feeds = discover_feeds(cfg, site)
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で残りを止めない
            print(f"NG  {site}  {failure_reason(exc)}")
            continue
        if not feeds:
            print(f"--  {site}  フィードの宣言なし（RSSを配信していない可能性）")
            continue
        print(f"OK  {site}")
        for feed_url, label in feeds:
            print(f"      {feed_url}   ({label})")
            found.append(feed_url)
            total += 1

    if not total:
        return 0
    if not args.probe:
        print(f"\n{total} 件見つかりました。probe-feed に渡して中身を確認してください:")
        print("  python -m freming.cli probe-feed " + " ".join(found))
        return 0

    # 見つけたフィードをそのまま試す。フィード1回ずつのリクエストで済むので、
    # 「URLを探す」と「中身を見る」を人が2回に分ける理由がない。
    print(f"\n{total} 件を続けて調べます。")
    probe_args = argparse.Namespace(
        config=args.config, url=found, limit=None, top=15, details=args.details
    )
    return _cmd_probe_feed(probe_args)


def _cmd_survey_sources(args: argparse.Namespace) -> int:
    """収集候補の robots.txt をまとめて調べる。

    確かめられるのは robots の層だけ。利用規約と掲載画像の権利は人が読んで
    判断する。混同すると「robotsがOK＝収集してよい」と誤読されるので、
    survey 側でも毎回その断りを出している。
    """
    from freming.collect.survey import main as survey_main

    argv: list[str] = []
    if args.file:
        argv += ["--file", str(args.file)]
    if args.csv:
        argv += ["--csv", str(args.csv)]
    argv += args.url
    return survey_main(argv)


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


def _cmd_remove(args: argparse.Namespace) -> int:
    """誤って取り込んだ候補を削除する（納品済みは対象外）。"""
    from freming.db.connection import session
    from freming.db.repository import delete_properties

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    with session(cfg.app.target()) as conn:
        removed = delete_properties(conn, source=args.source, property_id=args.id)
    print(f"{removed} 件削除しました")
    return 0


def _cmd_reset_images(args: argparse.Namespace) -> int:
    """取得済み画像を捨てて取り直せるようにする（抽出ルールを直したとき用）。"""
    import shutil
    from pathlib import Path

    from freming.db.connection import session
    from freming.db.repository import clear_images

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    with session(cfg.app.target()) as conn:
        removed = clear_images(conn, args.id)
    if removed == 0:
        print("対象がありません（存在しないIDか、納品済みです）", file=sys.stderr)
        return 1

    work_dir = Path(cfg.images.work_dir) / f"p{args.id:06d}"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    print(f"property_id={args.id} の画像 {removed} 件を消しました。deliver で取り直せます。")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from freming.db.connection import connect
    from freming.db.repository import count_by_status

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    conn = connect(cfg.app.target())
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

    p_sources = sub.add_parser("sources", help="編集ソースの key を並べる（定期実行用）")
    p_sources.add_argument("--enabled", action="store_true", help="有効なものだけ")
    p_sources.add_argument("--verbose", action="store_true", help="状態と名前も表示")
    p_sources.set_defaults(func=_cmd_sources)

    p_probe = sub.add_parser(
        "probe-feed", help="任意のフィードURLを試して判定内訳だけ表示（DBに書き込まない）"
    )
    p_probe.add_argument("url", nargs="+", help="調べるフィードURL（複数可）")
    p_probe.add_argument("--limit", type=int, default=None)
    p_probe.add_argument("--top", type=int, default=15)
    p_probe.add_argument("--details", action="store_true", help="各フィードの判定内訳も表示")
    p_probe.set_defaults(func=_cmd_probe_feed)

    p_learn = sub.add_parser("learn", help="非承認理由を分類しルール候補を作る（[7]）")
    p_learn.add_argument("--limit", type=int, default=None, help="一度に分類する件数")
    p_learn.set_defaults(func=_cmd_learn)

    p_rules = sub.add_parser("rules", help="ルール候補の確認と承認（[7]）")
    p_rules.add_argument("rules_action", choices=["list", "approve", "dismiss"])
    p_rules.add_argument("tag", nargs="?", help="approve / dismiss で指定するタグ")
    p_rules.set_defaults(func=_cmd_rules)

    p_deliver = sub.add_parser("deliver", help="承認済みを画像取得→加工→Drive納品（[4][5][6]）")
    p_deliver.add_argument("--limit", type=int, default=None)
    p_deliver.add_argument(
        "--watch", action="store_true",
        help="承認済みを拾い続ける（serve を起動していれば不要）",
    )
    p_deliver.add_argument(
        "--dry-run", action="store_true", help="Driveに書き込まず画像取得・加工まで実行"
    )
    p_deliver.set_defaults(func=_cmd_deliver)

    p_survey = sub.add_parser(
        "survey-sources", help="収集候補の robots.txt をまとめて調べる"
    )
    p_survey.add_argument("url", nargs="*")
    p_survey.add_argument("--file", type=Path, help="エリア/サイト名/URL のTSV")
    p_survey.add_argument("--csv", type=Path, help="結果の書き出し先")
    p_survey.set_defaults(func=_cmd_survey_sources)

    p_serve = sub.add_parser("serve", help="審査UIを起動（[3]）")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=_cmd_serve)

    p_check_api = sub.add_parser("check-api", help="スコアリングAPIの疎通確認")
    p_check_api.set_defaults(func=_cmd_check_api)

    p_score = sub.add_parser("score", help="未採点の候補をスコアリング（[2]）")
    p_score.add_argument("--limit", type=int, default=None)
    p_score.add_argument("--dry-run", action="store_true", help="DBに書き込まず結果だけ表示")
    p_score.set_defaults(func=_cmd_score)

    p_discover = sub.add_parser(
        "discover-feed", help="サイトのトップページから公開フィードURLを探す"
    )
    p_discover.add_argument("url", nargs="+", help="調べるサイトのURL（複数可）")
    p_discover.add_argument(
        "--probe", action="store_true", help="見つけたフィードをそのまま probe-feed で試す"
    )
    p_discover.add_argument(
        "--details", action="store_true", help="--probe 時に判定内訳とURLパターンも表示"
    )
    p_discover.set_defaults(func=_cmd_discover_feed)

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

    p_remove = sub.add_parser("remove", help="誤って取り込んだ候補を削除（納品済みは除く）")
    group = p_remove.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", help="ソースキー（例: circa_old_houses）")
    group.add_argument("--id", type=int, help="property_id")
    p_remove.set_defaults(func=_cmd_remove)

    p_reset_img = sub.add_parser(
        "reset-images", help="取得済み画像を捨てて取り直す（抽出ルールを直したとき）"
    )
    p_reset_img.add_argument("--id", type=int, required=True, help="property_id")
    p_reset_img.set_defaults(func=_cmd_reset_images)

    p_status = sub.add_parser("status", help="候補の件数をステータス別に表示")
    p_status.set_defaults(func=_cmd_status)

    return parser


# DBの列が揃っていないと動かないコマンド。列が足りないまま実行すると
# 「no such column」で落ち、原因が分かりにくいので手前で止める。
_NEEDS_MIGRATED_DB = frozenset({
    "collect", "score", "serve", "deliver", "learn", "rules",
    "ingest-url", "add-manual", "remove", "reset-images", "status",
})


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in _NEEDS_MIGRATED_DB:
        from freming.db.migrate import PendingMigrations, ensure_migrated

        try:
            ensure_migrated(load_config(args.config).app.target())
        except PendingMigrations as exc:
            print(str(exc), file=sys.stderr)
            return 2

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
