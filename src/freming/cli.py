"""CLI 入口。各モジュールは単体でも実行できるが、ここから一括で呼べる。

    freming check-drive            # Drive 疎通確認（最優先）
    freming db migrate             # マイグレーション適用
    freming db status              # 適用状況
    freming db check               # 接続先の疎通と中身（移行前の確認用）
    freming db transfer            # SQLite → PostgreSQL の移行（1回きり）

（collect / score / serve / deliver は各フェーズの実装時に追加する）
"""

from __future__ import annotations

import argparse
import os
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
    from freming.db.connection import connect as connect_db

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    db_path = args.db or cfg.app.target()

    if args.db_action == "status":
        for version, applied in migrate_mod.status(db_path):
            print(f"{'[x]' if applied else '[ ]'} {version}")
        return 0

    if args.db_action == "backfill-values":
        # 0008 で足した price_value / price_currency / year_built_value を埋める。
        # 書式（"$1,250,000" / "3,980 萬" / "built in 1902"）は SQL では解けない
        # ので、Python で読み直して書く。並べ替えと築年の足切りが使う。
        from freming.db.connection import session
        from freming.db.repository import backfill_values

        with session(cfg.app.target()) as conn:
            count = backfill_values(conn)
        print(f"{count} 件の価格・築年を数値化しました")
        return 0

    if args.db_action == "check":
        # 移行の前に接続文字列を確かめる。移行は1回きりなので、
        # 繋がるか・空かをここで見てから走らせる。
        from freming.db.dialect import POSTGRES, dialect_of, redact
        from freming.db.transfer import TABLES

        target = args.db or cfg.app.target()
        print(f"接続先: {redact(target)}")
        print(f"方言:   {'PostgreSQL' if dialect_of(target) == POSTGRES else 'SQLite'}")
        try:
            conn = connect_db(target)
        except Exception as exc:  # noqa: BLE001 - 原因をそのまま見せる
            print(f"接続できません: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        try:
            if dialect_of(target) == POSTGRES:
                row = conn.execute("SELECT version() AS v").fetchone()
                print(f"サーバ: {row['v'].split(' on ')[0]}")

            applied = [v for v, ok in migrate_mod.status(target) if ok]
            pending = [v for v, ok in migrate_mod.status(target) if not ok]
            print(f"マイグレーション: 適用済み {len(applied)} / 未適用 {len(pending)}")

            total = 0
            for table in TABLES:
                try:
                    count = conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table}"  # 定数の表名
                    ).fetchone()["n"]
                except Exception:  # noqa: BLE001 - テーブルが無い＝未適用
                    continue
                total += count
                if count:
                    print(f"  {table:<16} {count:>6} 行")
            if total == 0:
                print("空です。db transfer の移行先として使えます。")
            else:
                print(
                    f"既に {total} 行あります。db transfer は空のDBにしか流せません。"
                )
        finally:
            conn.close()
        return 0

    if args.db_action == "transfer":
        # SQLite から PostgreSQL への1回きりの移行。移行先は DATABASE_URL、
        # 移行元は config の db_path（--db で上書きできる）。
        # 移行先が空でないと止まるので、取り違えても二重投入にはならない。
        from freming.db.dialect import POSTGRES, dialect_of, redact
        from freming.db.transfer import TransferError, transfer

        dest = os.environ.get("DATABASE_URL", "")
        if not dest:
            print(
                "DATABASE_URL が未設定です。移行先の接続文字列を .env か環境変数に"
                "設定してください。",
                file=sys.stderr,
            )
            return 2
        if dialect_of(dest) != POSTGRES:
            print(f"DATABASE_URL が PostgreSQL ではありません: {redact(dest)}", file=sys.stderr)
            return 2

        source = Path(args.db) if args.db else cfg.app.db_path
        if not Path(source).exists():
            print(f"移行元が見つかりません: {source}", file=sys.stderr)
            return 2

        print(f"{source} → {redact(dest)}")
        try:
            stats = transfer(source, dest)
        except TransferError as exc:
            print(f"移行できませんでした: {exc}", file=sys.stderr)
            return 1
        print(stats.summary())
        return 0

    migrate_mod.migrate(db_path)
    print(f"OK: {db_path}")
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    from freming.collect.editorial import collect_source

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    # 販売ソース（経路A）は sitemap 起点で、判定の作りが経路Bと違う。
    # key で振り分ける。呼ぶ側が経路を意識せずに済むようにしておく。
    if cfg.editorial_source(args.source) is None and cfg.listing_source(args.source):
        from freming.collect.listings import collect_listing_source

        listing_stats = collect_listing_source(
            cfg, args.source, args.limit, args.dry_run, args.explain
        )
        print(listing_stats.report())
        if args.explain and listing_stats.samples:
            print("\n拾った物件:")
            print("\n".join(listing_stats.samples))
        if args.explain and listing_stats.no_price_samples:
            print("\n価格を取れなかったURL:")
            for u in listing_stats.no_price_samples:
                print(f"  {u}")
        return 0

    stats = collect_source(
        cfg, args.source, args.limit, args.dry_run, args.explain,
        backfill=not args.no_backfill,
    )
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
    # 販売ソースのうち mode: crawl のものも同じ扱いで並べる。定期実行は
    # この一覧を回して collect を叩くので、ここに出ないソースは
    # 有効にしても毎日の収集に乗らない。manual_only は自動収集しないので出さない。
    crawlable = [s for s in cfg.listing_sources if s.mode == "crawl"]
    for source in [*cfg.editorial_sources, *crawlable]:
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
    from freming.delivery.lock import DeliveryInProgress, delivery_lock

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
    if args.dry_run:
        # Drive に書かないので、他の納品と衝突しない。
        with session(cfg.app.target()) as conn:
            stats = deliver_approved(cfg, conn, args.limit, args.dry_run)
    else:
        try:
            with delivery_lock(cfg):
                with session(cfg.app.target()) as conn:
                    stats = deliver_approved(cfg, conn, args.limit, args.dry_run)
        except DeliveryInProgress as exc:
            # 定期実行から呼ばれる経路なので、これは失敗ではない。
            print(str(exc))
            return 0
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
    """審査UIを起動する（[3]）。ローカル利用なら認証は要らない。

    ループバック以外で待ち受けるときだけ Basic 認証を必須にする。
    """
    import uvicorn

    from freming.web.app import create_app
    from freming.web.auth import require_credentials

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    host = args.host or cfg.review_ui.host
    port = args.port or cfg.review_ui.port
    try:
        auth = require_credentials(host)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if auth is not None:
        print(f"認証: ON（ユーザ {auth.user}）")
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
    uvicorn.run(
        create_app(cfg, auth=auth), host=host, port=port, log_level=cfg.app.log_level.lower()
    )
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


def _photoless_ids(cfg, conn) -> list[int]:
    """代表画像が無い／単色のプレースホルダである候補のIDを集める。

    物件情報サイトは写真が未登録の物件にも og:image を返す。中身は単色の
    板で、寸法だけは本物と同じことが多い（Dream Town は 1280x800 の
    #D0D0D0）。URLの有無では判別できないので、実際に取得して中身を見る。

    取得に失敗したものは対象にしない。消す判断は「確かに絵が無かった」と
    言えるときだけにする。
    """
    from freming.db.repository import properties_for_photo_audit
    from freming.images.placeholder import is_flat_image
    from freming.net.client import HttpClient, RobotsDisallowed

    rows = properties_for_photo_audit(conn)
    found: list[int] = []
    with HttpClient(cfg.http) as client:
        for row in rows:
            url = row["thumbnail_url"]
            if not url:
                print(f"  画像URLなし  #{row['id']} {row['source']} {row['title'] or ''}"[:110])
                found.append(row["id"])
                continue
            try:
                response = client.get(url)
            except (RobotsDisallowed, Exception):  # noqa: BLE001 - 判定不能は残す
                print(f"  取得できず候補に残す  #{row['id']} {url}"[:110])
                continue
            if is_flat_image(response.content, cfg.images.flat_stddev_max):
                print(f"  単色画像    #{row['id']} {row['source']} {row['title'] or ''}"[:110])
                found.append(row["id"])
    return found


def _cmd_remove(args: argparse.Namespace) -> int:
    """誤って取り込んだ候補を削除する（納品済みは対象外）。"""
    from freming.db.connection import session
    from freming.db.repository import deletable_properties, delete_properties

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    with session(cfg.app.target()) as conn:
        ids = None
        if args.no_photo:
            print("代表画像を検査します（納品済みは対象外）…")
            ids = _photoless_ids(cfg, conn)
            if not ids:
                print("画像の無い候補はありませんでした")
                return 0

        targets = deletable_properties(
            conn, source=args.source, property_id=args.id, ids=ids
        )
        if not targets:
            print("対象がありません")
            return 0

        if args.dry_run:
            print(f"\n削除対象 {len(targets)} 件（--dry-run なので消していません）:")
            for row in targets:
                print(f"  #{row['id']:<5} {row['source']:<14} {row['title'] or row['source_url']}"[:120])
            return 0

        removed = delete_properties(
            conn, source=args.source, property_id=args.id, ids=ids
        )
    print(f"{removed} 件削除しました")
    return 0


def _refetch_targets(conn, cfg, source: str | None, limit: int | None):
    """枚数が上限に届いていない物件を、投稿に近い順に返す。

    納品済みを先に見る。**そこが投稿に回るので、直す価値が一番高い。**

    **承認済みと納品済みだけを対象にする。** 画像は承認してから取りに
    行くので、未審査・非承認の行は「足りない」のではなく最初から0枚。
    それを混ぜると、出す予定の無い候補のために相手サイトを何百回も
    叩くことになる（実測で 384 件が並び、うち大半が0枚だった）。
    """
    # 手で作った物件（source_url が manual: の印）は読み直す先が無い。
    where = [
        "p.source_url LIKE 'http%'",
        "p.status IN ('approved', 'delivered')",
    ]
    params: list = [cfg.images.max_per_property]
    if source:
        where.append("p.source = ?")
        params.append(source)
    sql = (
        "SELECT p.* FROM properties p "
        "WHERE (SELECT COUNT(*) FROM images i WHERE i.property_id = p.id) < ? "
        f"AND {' AND '.join(where)} "
        "ORDER BY p.status = 'delivered' DESC, p.status = 'approved' DESC, "
        "p.score DESC, p.id DESC"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, tuple(params)).fetchall()


def _cmd_refetch_images(args: argparse.Namespace) -> int:
    """掲載ページを読み直して、**足りない画像を足す**。既存は消さない。

    抽出を直したあとに使う。`reset-images` は納品済みを対象外にして
    いる（Drive の中身と食い違うため）が、こちらは足すだけなので
    納品済みでも通す。

    **Drive の納品フォルダは更新しない。** 増えるのは投稿に使う分だけで、
    Drive には最初に納品した枚数が残る。Instagram の投稿は images の行を
    見て組み立て、手元にファイルが無ければ取得元から取り直すので、
    ここで行が増えればカルーセルの枚数が増える。

    --source を渡すと、そのソースで**上限に届いていない物件**をまとめて
    直す。1件ずつ、相手サイトへの間隔を守って回る（HttpClient 任せ）。

    一度弾いたURLは image_skips に残る限り取りに行かない。基準そのものを
    変えたときは `reset-images --stale-skips` を先に。
    """
    from freming.db.connection import session
    from freming.images.fetch import NoImagesFound, fetch_images
    from freming.net.client import HttpClient

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    with session(cfg.app.target()) as conn:
        if args.id:
            rows = conn.execute(
                "SELECT * FROM properties WHERE id = ?", (args.id,)
            ).fetchall()
            if not rows:
                print(f"property {args.id} がありません。", file=sys.stderr)
                return 1
        else:
            rows = _refetch_targets(conn, cfg, args.source, args.limit)
            if not rows:
                print("上限に届いていない物件はありません。")
                return 0

        def _count(property_id: int) -> int:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM images WHERE property_id = ?", (property_id,)
            ).fetchone()["n"]

        print(f"対象 {len(rows)} 件（上限 {cfg.images.max_per_property} 枚）")
        for row in rows[:40]:
            print(f"  {row['id']:>5}  {_count(int(row['id'])):>2}枚  "
                  f"{(row['display_name'] or row['title'] or '')[:48]}")
        if len(rows) > 40:
            print(f"  …ほか {len(rows) - 40} 件")
        if args.dry_run:
            wait = len(rows) * cfg.http.request_interval_sec
            print(f"\n読み直しません（--dry-run）。所要 約 {wait / 60:.0f} 分＋画像の枚数分。")
            return 0

        gained = failed = 0
        with HttpClient(cfg.http) as client:
            for row in rows:
                property_id = int(row["id"])
                before = _count(property_id)
                try:
                    fetch_images(cfg, conn, row, client=client)
                except NoImagesFound as exc:
                    log_line = f"  {property_id:>5}  取れず（{exc}）"
                    print(log_line[:150], file=sys.stderr)
                    failed += 1
                    continue
                except Exception as exc:  # noqa: BLE001 - 1件で全体を止めない
                    print(f"  {property_id:>5}  失敗（{exc}）"[:150], file=sys.stderr)
                    failed += 1
                    continue
                after = _count(property_id)
                if after > before:
                    gained += after - before
                    print(f"  {property_id:>5}  {before} → {after} 枚")

    print(f"\n{len(rows)} 件を読み直し、{gained} 枚増えました"
          + (f"（{failed} 件は取れず）" if failed else ""))
    if gained:
        print("**Drive の納品フォルダは更新していません。** 増えたのは投稿に使う分です。")
    return 0


def _cmd_image_report(args: argparse.Namespace) -> int:
    """1物件の画像の内訳を出す。**枚数が足りない理由を目で確かめるため。**

    「3枚しか無い」には2つの原因があり、対処がまったく違う。

      1. 掲載ページに元々3枚しか無い → こちらでできることは無い
      2. 取ったが弾かれた（小さすぎる・形式違い・取得失敗）→ 基準を
         見直せば増える（reset-images --stale-skips）

    images と image_skips を並べれば、どちらかがその場で分かる。
    読むだけで、外部への通信はしない。
    """
    from freming.db.connection import session

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    with session(cfg.app.target()) as conn:
        property_id = args.id
        if property_id is None:
            # 予定の一覧（post show）に出るのは post_id なので、そこから
            # 引けるようにしておく。物件IDを別の画面で探させない。
            post = conn.execute(
                "SELECT property_id FROM posts WHERE id = ?", (args.post_id,)
            ).fetchone()
            if post is None or post["property_id"] is None:
                print(f"post {args.post_id} に物件が紐づいていません。", file=sys.stderr)
                return 1
            property_id = int(post["property_id"])
        row = conn.execute(
            "SELECT id, title, display_name, source, source_url, listing_url, status "
            "FROM properties WHERE id = ?", (property_id,),
        ).fetchone()
        if row is None:
            print(f"property {property_id} がありません。", file=sys.stderr)
            return 1
        images = conn.execute(
            "SELECT position, width, height, source_url FROM images "
            "WHERE property_id = ? ORDER BY position", (property_id,),
        ).fetchall()
        skips = conn.execute(
            "SELECT reason, source_url FROM image_skips WHERE property_id = ? "
            "ORDER BY reason, id", (property_id,),
        ).fetchall()

    print(f"property {row['id']}  {row['display_name'] or row['title']}（{row['source']} / {row['status']}）")
    print(f"  引用元: {row['source_url']}")
    if row["listing_url"]:
        print(f"  販売ページ: {row['listing_url']}")

    print(f"\n採用 {len(images)} 枚（上限 {cfg.images.max_per_property}）")
    for image in images:
        size = f"{image['width']}x{image['height']}" if image["width"] else "寸法不明"
        print(f"  {image['position'] or '-':>2}  {size:>10}  {image['source_url'][:78]}")

    if not skips:
        # **「弾いた記録が無い」は「元々無い」の証明にならない。**
        # 上限に達して打ち切った場合も、そもそも抽出できていない場合も、
        # image_skips には何も残らない。断定せずに次の手を出す。
        if len(images) >= cfg.images.max_per_property:
            print("\n弾いた画像はありません。**上限に達して打ち切っています。**"
                  "掲載ページにはこれより多くある可能性があります。")
        else:
            print("\n弾いた画像はありません。掲載ページに元々この枚数しか無いか、"
                  "**抽出が拾えていない**かのどちらかです。")
            print("  確かめるには: python -m freming.cli refetch-images --id "
                  f"{row['id']}")
        return 0

    counts: dict[str, int] = {}
    for skip in skips:
        counts[skip["reason"]] = counts.get(skip["reason"], 0) + 1
    print(f"\n弾いた {len(skips)} 枚: " + " / ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    for skip in skips[:20]:
        print(f"  {skip['reason']:<10} {skip['source_url'][:78]}")
    if len(skips) > 20:
        print(f"  …ほか {len(skips) - 20} 件")
    if counts.get("too_small") or counts.get("wrong_type"):
        print("\ntoo_small / wrong_type は基準を変えれば復活しえます:")
        print("  python -m freming.cli reset-images --stale-skips")
    return 0


def _cmd_reset_images(args: argparse.Namespace) -> int:
    """取得済み画像を捨てて取り直せるようにする（抽出ルールを直したとき用）。"""
    import shutil
    from pathlib import Path

    from freming.db.connection import session
    from freming.db.repository import clear_images, clear_stale_skips

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    if args.stale_skips:
        # min_short_edge_px / allowed_content_types を変えたあとに使う。
        # 一度弾いたURLは記録が残る限り二度と取りに行かないので、基準を
        # 下げても既存の物件は復活しない。ここで記録だけ落とす。
        with session(cfg.app.target()) as conn:
            removed = clear_stale_skips(conn)
        print(f"基準の変更で結論が変わりうる除外記録を {removed} 件消しました。")
        print("次の deliver で取り直します（取得できなかった記録は残しています）。")
        return 0

    with session(cfg.app.target()) as conn:
        removed = clear_images(conn, args.id)
    if removed == 0:
        print("消すものがありません（存在しないIDか、納品済みです）", file=sys.stderr)
        return 1

    work_dir = Path(cfg.images.work_dir) / f"p{args.id:06d}"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    print(
        f"property_id={args.id} の画像と除外記録を {removed} 件消しました。"
        "deliver で取り直せます。"
    )
    return 0



def _cmd_instagram(args: argparse.Namespace) -> int:
    """Instagram のトークン管理（[8] 自動投稿の土台）。

    トークンは60日で失効し、24時間経過後から更新できる。定期実行が毎日
    refresh を呼ぶので、set-token を1回通せば以後は自動で維持される。
    """
    from freming.db.connection import session
    from freming.instagram import tokens as ig

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    target = cfg.app.target()

    if args.ig_action == "set-token":
        import getpass

        # 引数やパイプで渡させない。シェル履歴・画面共有・チャットに残さない。
        value = getpass.getpass("長期アクセストークンを貼り付けて Enter（画面には表示されません）: ").strip()
        if not value:
            print("入力が空です。", file=sys.stderr)
            return 2
        try:
            profile = ig.fetch_profile(value)
        except ig.InstagramError as exc:
            print(f"トークンの確認に失敗しました: {exc}", file=sys.stderr)
            return 1
        with session(target) as conn:
            ig.save_token(conn, value)
        print(f"保存しました: @{profile.get('username')}")
        print("定期実行が毎日更新するので、以後の手作業は不要です。")
        return 0

    if args.ig_action == "auth-url":
        # @frmg.jpn の管理者に送るリンク。秘密情報は入らない（アプリIDは公開値）。
        app_id = cfg.instagram.app_id
        if not app_id:
            print("config.yaml の instagram.app_id が未設定です。", file=sys.stderr)
            return 2
        url = ig.authorization_url(app_id, args.redirect_uri)
        print("このURLを @frmg.jpn の管理者に送ってください:\n")
        print(f"  {url}\n")
        print("管理者が自分の端末で開いてログイン・許可すると、着地先の画面に")
        print("引き換え用の文字列（code）が出ます。それを受け取ったら:")
        print("  python -m freming.cli instagram exchange-code")
        return 0

    if args.ig_action == "exchange-code":
        import getpass

        app_id = cfg.instagram.app_id
        if not app_id:
            print("config.yaml の instagram.app_id が未設定です。", file=sys.stderr)
            return 2
        # code は使い捨てだが、app secret は使い回される。どちらも履歴に残さない。
        #
        # 環境変数からも受け取る。**ターミナルが無い人が GitHub Actions から
        # 実行できるようにするため。** 手元で叩くときは今までどおり聞かれる。
        code = os.environ.get("INSTAGRAM_AUTH_CODE") or getpass.getpass(
            "管理者から受け取った code を貼り付けて Enter: "
        )
        code = code.strip()
        secret = os.environ.get("INSTAGRAM_APP_SECRET") or getpass.getpass(
            "Instagram app secret を貼り付けて Enter（画面には表示されません）: "
        ).strip()
        if not code or not secret:
            print("入力が空です。", file=sys.stderr)
            return 2

        # 何を送ったのかが見えないと切り分けられない。code そのものは出さず、
        # 長さと前後だけ出す（貼り付けが途中で切れていないかの確認用）。
        cleaned = ig.clean_code(code)
        print(f"  code: {len(cleaned)}文字 {cleaned[:6]}…{cleaned[-4:]}")
        print(f"  redirect_uri: {args.redirect_uri}")
        print(f"  client_id: {app_id}")
        # **app secret の中身は出さない。** 出すのは形だけ。Meta の app secret は
        # 16進32文字と決まっているので、これだけで「別の欄を貼った」が分かる。
        # 実際、2026-09-01 の3回の失敗はどれも同じ文言で、code は形が見えていた
        # のに secret だけ見えず、切り分けに半日かかった。
        shape = "16進32文字" if len(secret) == 32 and all(
            c in "0123456789abcdefABCDEF" for c in secret
        ) else "**Meta の app secret の形ではありません**"
        print(f"  app secret: {len(secret)}文字 {shape}")

        try:
            value = ig.exchange_code(cleaned, app_id, secret, args.redirect_uri)
            profile = ig.fetch_profile(value)
        except ig.InstagramError as exc:
            print(f"引き換えに失敗しました: {exc}", file=sys.stderr)
            print(
                "\nこのエラーは次のどれでも同じ文言になります:\n"
                "  1. code が期限切れ（発行から1時間ほど）または使用済み\n"
                "  2. redirect_uri が認可時と1文字でも違う\n"
                "     → 上に出ている値と、Metaのダッシュボードの登録値を見比べてください\n"
                "     → 違うときは --redirect-uri で合わせられます\n"
                "  3. app secret が違う\n"
                "     → 上の「app secret」の形を見てください\n"
                "     → **Facebook アプリの「アプリシークレット」ではありません。**\n"
                "       Meta の画面で「Instagram」→「API設定」→ Instagram アプリの\n"
                "       シークレットを使ってください（別物です）\n\n"
                "1が濃厚なら、担当者さまに同じリンクをもう一度開いてもらってください"
                "（招待の承認はやり直し不要です）。",
                file=sys.stderr,
            )
            return 1
        with session(target) as conn:
            ig.save_token(conn, value)
        print(f"保存しました: @{profile.get('username')}")
        print("定期実行が毎日更新するので、以後の手作業は不要です。")
        return 0

    if args.ig_action == "check":
        from datetime import UTC, datetime

        with session(target) as conn:
            record = ig.load_token(conn)
        if record is None:
            print(
                "トークンが未設定です。次を実行してください:\n"
                "  python -m freming.cli instagram set-token",
                file=sys.stderr,
            )
            return 2
        try:
            profile = ig.fetch_profile(record.value)
        except ig.InstagramError as exc:
            print(f"疎通NG: {exc}", file=sys.stderr)
            return 1
        days = record.days_left(datetime.now(UTC))
        print(f"疎通OK: @{profile.get('username')}（トークン残り {days:.0f} 日）")
        if days < 7:
            print("残りが7日を切っています。定期実行の refresh が動いているか確認してください。")

        # 権限まで見る。**疎通が通っただけでは週次リールが動くか分からない。**
        # 再認可の直後にここで分かれば、担当者にもう一度お願いする判断が早い。
        from freming.instagram.insights import SCOPE, has_insights_scope

        try:
            granted = has_insights_scope(record.value)
        except ig.InstagramError as exc:
            print(f"リーチの権限: 判定できませんでした（{exc}）")
        else:
            if granted:
                print(f"リーチの権限: あり（{SCOPE}）。週次リールが動きます。")
            else:
                print(
                    f"リーチの権限: **なし**（{SCOPE}）。\n"
                    "  通常投稿は動きますが、週次リールは7枚を選べません。\n"
                    "  スコープはリフレッシュでは増やせないので、認可からやり直しが要ります:\n"
                    "    python -m freming.cli instagram auth-url"
                )
        return 0

    if args.ig_action in ("media", "fetch-media"):
        # **自分のアカウントの投稿だけ**を読む。手で運用していた頃の投稿を
        # 週次リールの材料にするための経路（instagram/mymedia.py）。
        from freming.instagram.mymedia import (
            download_image,
            get_media,
            recent_media,
        )
        from freming.instagram.publish import account_id

        with session(target) as conn:
            record = ig.load_token(conn)
        if record is None:
            print("トークンが未設定です。instagram set-token を先に。", file=sys.stderr)
            return 2
        try:
            ig_id = account_id(record.value)
        except ig.InstagramError as exc:
            print(f"アカウントを読めませんでした: {exc}", file=sys.stderr)
            return 1

        if args.ig_action == "media":
            try:
                items = recent_media(record.value, ig_id, args.limit)
            except ig.InstagramError as exc:
                print(f"投稿を読めませんでした: {exc}", file=sys.stderr)
                return 1
            if not items:
                print("投稿がありません。")
                return 0
            for item in items:
                when = item.timestamp[:16].replace("T", " ")
                pages = f"{item.child_count}枚" if item.child_count else item.media_type
                print(f"  {item.id}  {when}  {pages:<9} {item.head()}")
            print("\n画像を取るには:")
            print("  python -m freming.cli instagram fetch-media --id <ID> --out frames/")
            return 0

        if not args.id:
            print("--id で投稿のIDを指定してください（instagram media の先頭の値）。",
                  file=sys.stderr)
            return 2
        out = Path(args.out)
        saved = []
        for index, media_id in enumerate(args.id, start=1):
            try:
                item = get_media(record.value, media_id)
            except ig.InstagramError as exc:
                print(f"{media_id}: 読めませんでした（{exc}）", file=sys.stderr)
                return 1
            if not item.image_url:
                print(f"{media_id}: 画像が取れません（{item.media_type}）", file=sys.stderr)
                return 1
            # 並べる順が分かる名前にする。リールに渡すときこの順で並ぶ。
            dest = out / f"{index:02d}-{media_id}.jpg"
            try:
                download_image(item.image_url, dest)
            except Exception as exc:  # noqa: BLE001 - 原因をそのまま見せる
                print(f"{media_id}: 保存に失敗しました（{exc}）", file=sys.stderr)
                return 1
            saved.append(dest)
            print(f"  {dest}  {item.timestamp[:10]}  {item.head()}")
        print(f"\n{len(saved)} 枚保存しました。リールに渡すには:")
        print("  python -m freming.cli reel build " + " ".join(str(p) for p in saved)
              + " --out reel.mp4")
        return 0

    # refresh: 定期実行から毎日呼ばれる。未設定の環境では何もせず正常終了する。
    with session(target) as conn:
        try:
            outcome = ig.refresh_token(conn)
        except ig.InstagramError as exc:
            print(f"更新に失敗しました: {exc}", file=sys.stderr)
            return 1
    messages = {
        "no_token": "トークンが未設定のためスキップしました。",
        "too_new": "取得から24時間未満のためスキップしました（Meta側の制約）。",
        "refreshed": "トークンを更新しました（残り60日に戻りました）。",
        "expired": "トークンが失効しています。再認可して set-token をやり直してください。",
    }
    print(messages[outcome])
    return 1 if outcome == "expired" else 0


def _cmd_redeliver(args: argparse.Namespace) -> int:
    """納品の記録を取り消して、もう一度納品させる（[6]）。

    **Drive のフォルダはこちらでは消さない。** 消せる権限をこの経路に
    持ち込まない（納品先を壊す事故のほうが、フォルダが2つ残ることより重い）。
    人が Drive 側を片付けてから呼ぶ前提。

    フォルダ名は「既存の最大値＋1」で決まるので、**番号は元に戻らない**。
    frmg_ig006 を消して再納品しても 006 ではなく次の番号が振られる。
    """
    from freming.db.connection import session
    from freming.db.repository import undo_delivery

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    with session(cfg.app.target()) as conn:
        row = conn.execute(
            "SELECT p.title, d.folder_name, d.drive_folder_id "
            "FROM properties p JOIN deliveries d ON d.property_id = p.id "
            "WHERE p.id = ?",
            (args.property_id,),
        ).fetchone()
        if row is None:
            print(
                f"property_id={args.property_id} の納品記録がありません。"
                "（未納品か、IDが違います）",
                file=sys.stderr,
            )
            return 1

        print(f"  物件: {row['title']}")
        print(f"  Drive: {row['folder_name']}（folder_id={row['drive_folder_id']}）")
        if not args.yes:
            print(
                "\nこの納品記録を取り消して、承認済みに戻します。\n"
                "**Drive のフォルダは消えません。** 先に手で片付けてください。\n"
                "再納品では番号が変わります（同じ frmg_ig 番号には戻りません）。\n"
                "実行するには --yes を付けてもう一度。"
            )
            return 0

        folder = undo_delivery(conn, args.property_id)
    print(f"取り消しました（元: {folder}）。自動納品が次の巡回で拾います。")
    return 0


def _cmd_rescore(args: argparse.Namespace) -> int:
    """採点し直す（[2]）。基準を変えたあと、既存分にも当てるための経路。

    足切り（築年・story）は**採点した時点のルール**で効くので、config を
    変えても採点済みの行は動かない。ここで採点結果を消し、`score` が
    もう一度拾えるようにする。

    **APIの費用がかかる。** 何件をいくらで叩くのかを先に出し、--yes が
    無ければ何もしない。
    """
    from freming.db.connection import session
    from freming.db.repository import clear_scores, properties_needing_rescore

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    with session(cfg.app.target()) as conn:
        rows = properties_needing_rescore(conn, before=args.before, limit=args.limit)
        if not rows:
            print("採点し直す対象がありません。")
            return 0

        # 概算。本文の長さから入力トークンを見積もる（日本語・英語まじりで
        # おおよそ2.5文字＝1トークン）。出力は構造化された固定長に近い。
        chars = sum(len(r["content_text"] or "") for r in rows)
        in_tokens = chars / 2.5 + len(rows) * 1200      # 本文＋システムプロンプト
        out_tokens = len(rows) * 400
        cost = in_tokens / 1e6 * 1.0 + out_tokens / 1e6 * 5.0   # Haiku 4.5 の単価
        print(f"対象 {len(rows)} 件（納品済みは含みません）")
        print(f"  本文 合計 {chars:,} 字 → 入力 約 {in_tokens/1000:.0f}k / 出力 約 {out_tokens/1000:.0f}k トークン")
        print(f"  概算費用 **約 ${cost:.2f}**（{cfg.scoring.model} の単価。目安です）")

        if not args.yes:
            print("\n採点結果を消して未採点に戻します。実行するには --yes を付けてもう一度。")
            print("そのあと `python -m freming.cli score` で採点し直してください。")
            return 0

        cleared = clear_scores(conn, [int(r["id"]) for r in rows])
    print(f"{cleared} 件を未採点に戻しました。`score --limit {cleared}` で採点し直せます。")
    return 0


def _cmd_backfill_captions(args: argparse.Namespace) -> int:
    """[2] 投稿本文で使う項目を既存の物件に埋める。

    **rescore とは別物。** rescore は採点をやり直すもので、納品済みには
    触らない。こちらは納品済みも対象にし、score / status / summary には
    一切触らず、0012・0013 で足した列だけを埋める。
    """
    from freming.db.connection import session
    from freming.scoring.backfill import backfill, estimate, pending_rows

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    with session(cfg.app.target()) as conn:
        rows = pending_rows(conn, args.limit)
        if not rows:
            print("埋める対象はありません。")
            return 0
        tokens_in, tokens_out, cost = estimate(rows)
        delivered = sum(1 for r in rows if r["status"] == "delivered")
        no_text = sum(1 for r in rows if not (r["content_text"] or "").strip())
        print(f"対象 {len(rows)} 件（うち納品済み {delivered} 件）")
        if no_text:
            print(f"  うち {no_text} 件は本文が無いので埋まりません（APIも呼びません）")
        print(f"  入力 約 {tokens_in / 1000:.0f}k / 出力 約 {tokens_out / 1000:.0f}k トークン")
        print(f"  概算費用 **約 ${cost:.2f}**（{cfg.scoring.model} の単価。目安です）")
        if not args.yes:
            print(
                "\nスコア・審査結果・summary には触りません。足した列だけを埋めます。\n"
                "実行するには --yes を付けてもう一度。"
            )
            return 0
        stats = backfill(cfg, conn, args.limit)

    print(stats.summary())
    if stats.lines:
        print("\n".join(stats.lines[:40]))
    return 0


def _cmd_backfill_listings(args: argparse.Namespace) -> int:
    """[1] 記事の中にある販売ページのURLを、既存の物件に入れる。

    ストーリーズに貼るのは「その家が買えるページ」。記事の末尾にリンクが
    あるので、記事をもう一度読んで控える。**販売サイトへは行かない。**

    API費用はかからない（LLMを使わない）。かかるのは記事を取りに行く
    時間だけで、1件あたり3秒以上あける。
    """
    from freming.collect.relink import pending_rows, relink
    from freming.db.connection import session

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    with session(cfg.app.target()) as conn:
        rows = pending_rows(conn, args.limit)
        if not rows:
            print("販売ページを入れる対象はありません。")
            return 0
        delivered = sum(1 for r in rows if r["status"] == "delivered")
        wait = len(rows) * cfg.http.request_interval_sec
        print(f"対象 {len(rows)} 件（うち納品済み {delivered} 件）")
        print(f"  記事を1件ずつ読み直します。所要 約 {wait / 60:.0f} 分（API費用なし）")
        print("  読みに行くのは記事のページだけです。販売サイトへは接続しません。")
        if not args.yes:
            print("\n実行するには --yes を付けてもう一度。")
            return 0
        stats = relink(cfg, conn, args.limit)

    print(stats.summary())
    if stats.lines:
        print("\n".join(stats.lines[:40]))
    return 0


def _cmd_sources_report(args: argparse.Namespace) -> int:
    """ソース別の実績（[1]）。自動収集を続けるかの判断に使う。

    「何件入ったか」ではなく「**何件が承認まで行ったか**」を見る。
    たくさん入っても全部非承認なら、そのソースは採点の費用と審査の手間を
    食っているだけになる。
    """
    from freming.db.connection import session
    from freming.db.repository import source_outcomes

    cfg = load_config(args.config)
    with session(cfg.app.target()) as conn:
        rows = source_outcomes(conn)
    if not rows:
        print("まだ1件も入っていません。")
        return 0

    print(f"{'ソース':<18}{'収集':>6}{'採点':>6}{'未審査':>7}{'承認':>6}"
          f"{'非承認':>7}{'納品':>6}{'最高点':>8}  自動収集")
    for row in rows:
        key = row["source"]
        src = cfg.editorial_source(key) or cfg.listing_source(key)
        mode = "—"
        if src is not None:
            crawl = getattr(src, "mode", "crawl") == "crawl"
            mode = ("有効" if src.enabled else "無効") if crawl else "手動のみ"
        best = f"{row['best_score']:.0f}" if row["best_score"] is not None else "—"
        print(f"{key:<18}{row['collected']:>6}{row['scored'] or 0:>6}"
              f"{row['pending'] or 0:>7}{row['approved'] or 0:>6}"
              f"{row['rejected'] or 0:>7}{row['delivered'] or 0:>6}{best:>8}  {mode}")
    print("\n承認＋納品が 0 のまま件数だけ多いソースは、manual_only に落とすか無効化を検討する。")
    return 0


def _cmd_post(args: argparse.Namespace) -> int:
    """[9] Instagram への投稿。予定を作る / 見る / 時間が来たものを出す。

    自動投稿は審査UIの中で回るが、手で叩ける経路も残す。予定を作るのは
    定期実行から、投稿は審査UIから、という分け方になる。
    """
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from freming.db.connection import session
    from freming.db.repository import count_posts_by_state, scheduled_posts
    from freming.instagram.plan import plan
    from freming.instagram.worker import run_once

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    with session(cfg.app.target()) as conn:
        if args.post_action == "plan":
            stats = plan(cfg, conn)
            print(stats.summary())
            return 0

        if args.post_action == "replan":
            # 本文は予定を作った時点で組んで持っている。あとから項目を
            # 足しても既存の予定には反映されないので、出す前に作り直す。
            from freming.db.repository import planned_posts_with_property, set_caption
            from freming.instagram.caption import build_caption

            changed = 0
            for row in planned_posts_with_property(conn):
                src = cfg.editorial_source(row["source"]) or cfg.listing_source(row["source"])
                caption = build_caption(row, cfg.caption, src.name if src else None)
                if set_caption(conn, row["post_id"], caption):
                    changed += 1
            print(f"{changed} 件の本文を作り直しました（投稿済みは触っていません）。")
            return 0

        if args.post_action == "compact":
            from freming.instagram.plan import compact

            moved = compact(cfg, conn)
            print(f"{moved} 件を前に詰めました。" if moved else "詰める予定はありません。")
            return 0

        if args.post_action == "reschedule":
            # ワーカーが止まっていた間に、時刻を過ぎた予定が溜まる。
            # **そのまま動かすと数分のうちにまとめて出る。**
            #
            # 捨てずに先送りするのは、postable_properties が
            # 「その物件に feed の行があるか」で判定しているため。
            # 消すと二度と投稿候補に戻らない。
            from freming.db.repository import (
                set_scheduled_at,
                stale_planned_posts,
            )
            from freming.instagram.plan import next_reel_time, slot_times
            from freming.instagram.publish import KIND_FEED, KIND_REEL

            now = datetime.now(UTC)
            zone = ZoneInfo(cfg.instagram.timezone)
            slots = slot_times(cfg, now)
            slot_keys = {s.isoformat() for s in slots}
            until = (slots[-1] if slots else now) + timedelta(minutes=1)
            upcoming = scheduled_posts(conn, until.isoformat())

            # 動かす対象は2種類。**どちらも「そのままだと並びが崩れる」もの。**
            #   1. 時刻を過ぎたまま出ていない予定（ワーカーを止めていた間の分）
            #   2. いまの post_times に無い枠に載っている予定
            #      （1日3投稿から1投稿に減らしたときなど）
            targets = list(stale_planned_posts(conn, now.isoformat()))
            seen = {row["id"] for row in targets}
            for row in upcoming:
                if row["id"] in seen or row["state"] != "planned":
                    continue
                if row["kind"] != KIND_FEED or row["scheduled_at"] in slot_keys:
                    continue
                targets.append(row)
            targets.sort(key=lambda r: (r["scheduled_at"], r["id"]))
            if not targets:
                print("動かす予定はありません。")
                return 0

            moving = {row["id"] for row in targets}
            taken = {
                row["scheduled_at"] for row in upcoming
                if row["kind"] == KIND_FEED and row["id"] not in moving
                and row["scheduled_at"] >= now.isoformat()
            }
            free = [s for s in slots if s.isoformat() not in taken]

            moved = 0
            for row in targets:
                if row["kind"] == KIND_REEL:
                    # リールは曜日が決まっている。次の回に送る。
                    target = next_reel_time(cfg, now)
                elif free:
                    target = free.pop(0)
                else:
                    print(f'  空き枠が足りません: post {row["id"]}（{row["kind"]}）はそのまま')
                    continue
                if set_scheduled_at(conn, row["id"], target.isoformat()):
                    moved += 1
                    at = target.astimezone(zone)
                    was = datetime.fromisoformat(row["scheduled_at"]).astimezone(zone)
                    print(f'  post {row["id"]}  {row["kind"]:<5} '
                          f'{was:%m/%d %H:%M} → {at:%m/%d %H:%M}')
            print(f"{moved} 件を動かしました。")
            if len(targets) > moved:
                print(
                    f"{len(targets) - moved} 件は空き枠が無くて動かせていません。"
                    "plan_days を延ばすか、明日もう一度実行してください。"
                )
            return 0

        if args.post_action in ("skip", "unskip"):
            # 見送りと、その取り消し。審査UIからもできるが、requeue や run と
            # 同じ場所（ターミナル）で完結できないと、順番の操作を間違える。
            from freming.db.repository import retry_post, skip_post

            if not args.id:
                print("--id で投稿のIDを指定してください（post show の先頭の番号）。",
                      file=sys.stderr)
                return 2
            if args.post_action == "skip":
                if skip_post(conn, args.id):
                    print(f"post {args.id} を見送りにしました。post unskip --id で戻せます。")
                    return 0
                print(
                    f"post {args.id} は見送りにできません（予定/失敗の状態のみ）。",
                    file=sys.stderr,
                )
                return 2
            if retry_post(conn, args.id):
                print(f"post {args.id} を予定に戻しました。時刻が過ぎていれば "
                      "post reschedule で先の枠に入ります。")
                return 0
            print(f"post {args.id} は戻せません（見送り/失敗の状態のみ）。", file=sys.stderr)
            return 2

        if args.post_action == "requeue":
            # 出し直し。Instagram 側で消した投稿を、もう一度予定に戻す。
            # **先にアプリ側で削除してから使うこと。** 消さずに戻すと
            # 同じ物件が2回並ぶ。APIから削除はしない（消す判断は人がする）。
            if not args.id:
                print("--id で投稿のIDを指定してください（post show の post_id）。",
                      file=sys.stderr)
                return 2
            row = conn.execute(
                "SELECT * FROM posts WHERE id = ?", (args.id,)
            ).fetchone()
            if row is None:
                print(f"post {args.id} が見つかりません。", file=sys.stderr)
                return 2
            if row["kind"] != "feed" or row["state"] != "published":
                print(
                    f"post {args.id} は {row['kind']}/{row['state']} です。"
                    "出し直せるのは公開済みの通常投稿だけです。",
                    file=sys.stderr,
                )
                return 2
            zone = ZoneInfo(cfg.instagram.timezone)
            if args.now:
                # すぐ出したいとき。auto_post が動いていれば1分以内に出る。
                target = datetime.now(UTC)
            else:
                # 既定は**次の空き枠**。「今」に戻すと、自動投稿が動いている間は
                # 1分以内にワーカーが拾ってしまい、時刻を直す暇がない。
                from freming.instagram.plan import _parse_hhmm

                local_now = datetime.now(UTC).astimezone(zone)
                horizon = (datetime.now(UTC) + timedelta(days=31)).isoformat()
                taken = {
                    r["scheduled_at"] for r in scheduled_posts(conn, horizon)
                    if r["kind"] == "feed" and r["id"] != args.id
                    and r["state"] in ("planned", "publishing")
                }
                target = None
                for offset in range(31):
                    day = (local_now + timedelta(days=offset)).date()
                    for text in cfg.instagram.post_times:
                        moment = datetime.combine(day, _parse_hhmm(text), tzinfo=zone)
                        if moment <= local_now:
                            continue
                        as_utc = moment.astimezone(UTC)
                        if as_utc.isoformat() in taken:
                            continue
                        target = as_utc
                        break
                    if target is not None:
                        break
                if target is None:
                    print("31日先まで空き枠がありません。", file=sys.stderr)
                    return 1

            conn.execute(
                """
                UPDATE posts SET state = 'planned', scheduled_at = ?,
                    ig_media_id = NULL, ig_container_id = NULL, permalink = NULL,
                    attempts = 0, error = NULL, published_at = NULL
                WHERE id = ?
                """,
                (target.isoformat(), args.id),
            )
            conn.commit()
            local = target.astimezone(zone)
            when = "今すぐ" if args.now else f"{local:%m/%d %H:%M}"
            print(f"post {args.id} を予定に戻しました（{when}）。")
            print("本文を作り直すなら post replan。時刻は審査UIでも直せます。")
            print("**Instagram 側の元の投稿を消していなければ、先に消してください。**")
            return 0

        if args.post_action == "show":
            zone = ZoneInfo(cfg.instagram.timezone)
            days = args.days or cfg.instagram.plan_days
            until = datetime.now(UTC) + timedelta(days=days)
            rows = scheduled_posts(conn, until.isoformat())
            if not rows:
                print("予定はありません。post plan で作ってください。")
                return 0
            # 先頭の番号が post_id。requeue --id で使う。
            #
            # 枚数も出す。**カルーセルが何枚になるかは出してみるまで
            # 分からない**、では順番を整える判断ができない。上限
            # （carousel_max）に届かない行は目印を付ける。
            from freming.instagram import media as _media

            def _actual(row, slot) -> str:
                """実際に出た時刻と、枠からの遅れ。

                **「定刻に出たか」は published を見ても分からない。**
                published_at は持っているのに出していなかったので、
                毎朝の確認のたびに「何分遅れたか」が答えられなかった。
                """
                stamp = row["published_at"]
                if not stamp:
                    return ""
                when = datetime.fromisoformat(stamp).astimezone(zone)
                late = round((when - slot).total_seconds() / 60)
                mark = f"+{late}分" if late > 0 else "定刻"
                return f"{when:%H:%M} {mark}"

            cap = cfg.instagram.carousel_max
            for row in rows:
                at = datetime.fromisoformat(row["scheduled_at"]).astimezone(zone)
                title = row["title"] or ("週次リール" if row["kind"] == "reel" else "—")
                shots = ""
                if row["kind"] == "feed" and row["property_id"]:
                    count = len(_media.available_positions(conn, row["property_id"]))
                    count = min(count, cap)
                    shots = f'{count:>2}枚{"!" if count < cap else " "}'
                print(f'  {row["id"]:>4}  {at:%m/%d %H:%M}  '
                      f'{row["kind"]:<5} {row["state"]:<10} {shots:<5} '
                      f'{_actual(row, at):<12} {title[:40]}')
            counts = count_posts_by_state(conn)
            print("  " + " / ".join(f"{k} {v}" for k, v in sorted(counts.items())))
            # **合計はこの一覧の外の行も数えている。** 先の日付に置いた
            # リールなどが窓の外に落ちると、一覧と合計が食い違って見える。
            # 黙って隠すと「消えた」と読めるので、件数と出し方を書く。
            hidden = sum(counts.values()) - len(rows)
            if hidden > 0:
                print(f"  この先に、あと {hidden} 件あります（表示は {days} 日先まで）。"
                      f"すべて見るなら post show --days 60")
            return 0

        if not cfg.instagram.public_base_url:
            print(
                "instagram.public_base_url が未設定です。\n"
                "Meta は投稿のたびにこちらのサーバーへ画像を取りに来るので、"
                "審査UIの公開URLを config.yaml に設定してください。",
                file=sys.stderr,
            )
            return 1
        result = run_once(
            cfg, conn, limit=args.limit, dry_run=args.dry_run,
            kinds=tuple(args.kind) if args.kind else None,
        )
        done = result.done
        if done:
            if args.dry_run:
                print(f"{done} 件の中身を出しました（投稿していません）。上のログを確認してください。")
            else:
                print(f"{done} 件投稿しました。")
        elif not result.failed:
            print("時間が来ている予定はありません。")
        # **落ちたら赤で終わる。** ここを 0 で返していたので、2026-09-01 の
        # 初回リールが3回とも 400 で失敗したのに GitHub Actions は緑だった。
        # ログを開かないかぎり「出たはず」で通ってしまう。
        if result.failed:
            print(
                f"{result.failed} 件は投稿できませんでした。上のログを確認してください。",
                file=sys.stderr,
            )
            return 1
        return 0


def _publish_prebuilt_reel(cfg, args, result, track) -> int:
    """組み上がった動画をリールとして出す。

    自動の週次リールは「直近8日の日ごとの1位」を自分で選ぶ。こちらは
    **人が画像を選んだとき**の経路で、仕組みが始まる前の投稿を材料に
    混ぜられる。予定の行（post show の reel）に紐づけて、出したことを
    記録する。紐づけないと、同じ週にもう1本出てしまう。
    """
    from freming.db.connection import session
    from freming.db.repository import finish_post
    from freming.instagram import tokens as ig
    from freming.instagram.caption import build_reel_caption
    from freming.instagram.publish import account_id, publish_reel

    caption = build_reel_caption(result.image_count, cfg.caption, track.caption_line())
    if args.dry_run:
        print("\n--- 出しません（dry-run）。本文 ---")
        print(caption)
        print(f"\n動画: {result.path}")
        print("出すときは --dry-run を外してください。")
        return 0

    with session(cfg.app.target()) as conn:
        record = ig.load_token(conn)
        if record is None:
            print("トークンが未設定です。", file=sys.stderr)
            return 2
        post = None
        if args.post_id:
            post = conn.execute(
                "SELECT id, kind, state FROM posts WHERE id = ?", (args.post_id,)
            ).fetchone()
            if post is None:
                print(f"post {args.post_id} が見つかりません。", file=sys.stderr)
                return 2
            if post["kind"] != "reel":
                print(f"post {args.post_id} はリールではありません（{post['kind']}）。",
                      file=sys.stderr)
                return 2
            if post["state"] == "published":
                print(f"post {args.post_id} は既に公開済みです。", file=sys.stderr)
                return 2

        try:
            ig_id = account_id(record.value)
            published = publish_reel(record.value, ig_id, Path(args.out), caption)
        except ig.InstagramError as exc:
            print(f"投稿に失敗しました: {exc}", file=sys.stderr)
            return 1

        if post is not None:
            conn.execute(
                "UPDATE posts SET caption = ?, credit = ? WHERE id = ?",
                (caption, track.caption_line() or None, post["id"]),
            )
            finish_post(conn, post["id"], published.media_id, published.container_id)
            print(f"post {post['id']} を公開済みにしました。")
        else:
            print("**予定の行に紐づけていません。**「--post-id」を付けると、"
                  "同じ週に自動のリールが二重に出るのを防げます。")
    print(f"投稿しました: media_id={published.media_id}")
    return 0


def _cmd_reel_preview(cfg, args: argparse.Namespace) -> int:
    """今週の自動リールを、出さずに1本組んで中身を並べる。

    **出す処理とまったく同じ経路**（worker.build_weekly_reel）を通る。
    別に書くと、試写で見たものと出るものが食い違う。

    Meta へ送るものは無い。リーチの読み取りだけ API を叩く
    （権限が無ければ直近の投稿で代用する。実際に出るときと同じ挙動）。
    """
    from datetime import UTC, datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from freming.db.connection import session
    from freming.instagram.tokens import load_token
    from freming.instagram.worker import PostingError, build_weekly_reel
    from freming.reel.build import ReelError

    zone = ZoneInfo(cfg.instagram.timezone)
    out = Path(args.out if args.out != "reel.mp4" else "reel-preview.mp4")
    with session(cfg.app.target()) as conn:
        record = load_token(conn)
        if record is None:
            print("Instagram のトークンが未設定です。", file=sys.stderr)
            return 1
        try:
            built = build_weekly_reel(
                cfg, conn, record.value, out,
                allow_fallback=True if args.fallback else None,
            )
        except (PostingError, ReelError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    print(f"試写用に組みました: {built.video}（**投稿していません**）")
    print(f"  {built.result.image_count}枚 / {built.result.seconds:.1f}秒"
          f"（1枚 {built.result.per_image_sec:.2f}秒）")
    print(f"  音源: {built.track.title} — {built.track.artist}"
          f"（{built.track.license}）")
    line = built.track.caption_line()
    print(f"  クレジット: {line}" if line else "  クレジット表記は不要な曲です。")

    if built.picked_by == "recent":
        print("\n**選び方が本来と違います。** リーチを読む権限が無いので、"
              "日ごとの1位ではなく直近の投稿で代用しました。"
              "\n仕上がり（絵・尺・音・本文）は本番と同じですが、"
              "**どの投稿が入るかは変わりえます。**")
    label = "日ごとのリーチ1位" if built.picked_by == "reach" else "直近の投稿で代用"
    print(f"\n使った投稿 {len(built.winners)} 件（{label}・古い順）")
    # **物件名を出す。** posts の行だけでは何の写真か分からない
    # （posts に title は無い）。1枚目にどの家が来るかは、いちばん
    # 見たいところ。
    with session(cfg.app.target()) as conn:
        names = {}
        for row in built.winners:
            found = conn.execute(
                "SELECT display_name, title FROM properties WHERE id = ?",
                (row["property_id"],),
            ).fetchone()
            if found is not None:
                names[row["id"]] = found["display_name"] or found["title"] or ""

    for index, row in enumerate(built.winners, start=1):
        when = datetime.fromisoformat(row["published_at"]).astimezone(zone)
        reach = row["reach"]
        head = "（表紙）" if index == 1 else "　　　　"
        print(f"  {index}枚目{head}{when:%m/%d}  "
              f"リーチ {reach if reach is not None else '不明':>5}  "
              f"post {row['id']:>3}  {names.get(row['id'], '')[:40]}")

    print("\n--- 本文 ---")
    print(built.caption)
    print(f"\n（{len(built.caption)} 字 / 上限 2200）")
    print(f"組んだコマ: {out.parent / f'.{out.stem}-frames'}")
    print(f"いま {datetime.now(UTC).astimezone(zone):%m/%d %H:%M} 時点の並びです。"
          "**出すときにもう一度選び直す**ので、それまでに投稿が増えれば変わります。")
    return 0


def _cmd_reel(args: argparse.Namespace) -> int:
    """[9] 正方形の画像を並べて縦動画を1本作る。

    まだ投稿はしない。どの画像を渡すかは人が決める段階なので、
    画像のパスを引数で受ける。自動化は投稿処理と一緒に入れる。
    """
    from pathlib import Path

    from freming.reel.build import ReelError, audio_for_week, build_reel, load_tracks

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    if args.reel_action == "tracks":
        try:
            tracks = load_tracks()
        except ReelError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        for index, track in enumerate(tracks):
            mark = "要" if track.attribution_required else "不要"
            print(f"  {index + 1}週目  {track.title} — {track.artist}"
                  f"  [{track.license} / クレジット{mark}]")
        return 0

    if args.reel_action == "preview":
        return _cmd_reel_preview(cfg, args)

    squares = [Path(p) for p in args.images]
    missing = [p for p in squares if not p.exists()]
    if missing:
        print("画像が見つかりません: " + ", ".join(str(p) for p in missing), file=sys.stderr)
        return 1

    try:
        track = audio_for_week(args.week)
        result = build_reel(squares, track, Path(args.out), cfg.reel)
    except ReelError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.reel_action == "publish":
        return _publish_prebuilt_reel(cfg, args, result, track)

    print(f"作りました: {result.path}")
    print(f"  {result.image_count}枚 / {result.seconds:.1f}秒"
          f"（1枚 {result.per_image_sec:.2f}秒）")
    print(f"  音源: {track.title} — {track.artist}（{track.license}）")
    line = track.caption_line()
    print(f"  キャプションに入れる1行: {line}" if line
          else "  クレジット表記は不要です。")
    # 組んだコマは消さずに残す。仕上がりが変なときは、動画より先に
    # ここを見たほうが原因が早く分かる。
    print(f"  組んだコマ: {Path(args.out).parent / f'.{Path(args.out).stem}-frames'}")
    return 0


def _cmd_fx(args: argparse.Namespace) -> int:
    """円換算レートの取得と確認。

    価格の並べ替えにのみ使う（表示には出さない）。定期実行が毎日
    update を呼ぶが、実際に外へ出るのは7日を過ぎたときだけ。
    """
    from freming.db.connection import session
    from freming.fx import FxError, effective_rates, load_rates, update_rates

    cfg = load_config(args.config)
    setup_logging(cfg.app.log_dir, cfg.app.log_level)

    if args.fx_action == "show":
        with session(cfg.app.target()) as conn:
            rates, as_of = effective_rates(conn, cfg)
            stored, _ = load_rates(conn)
        source = "DB（自動更新）" if stored else "config.yaml（未取得のため）"
        print(f"出どころ: {source}")
        print(f"基準:     {as_of}")
        for code, value in sorted(rates.items()):
            print(f"  {code}  {value:>10,.2f} 円")
        return 0

    with session(cfg.app.target()) as conn:
        try:
            outcome = update_rates(conn, cfg, force=args.force)
            rates, as_of = effective_rates(conn, cfg)
        except FxError as exc:
            # 取れなくても収集や採点とは無関係なので、定期実行は止めない。
            print(f"レートを更新できませんでした: {exc}", file=sys.stderr)
            return 1
    if outcome == "fresh":
        print(f"レートはまだ新しいので取得しませんでした（{as_of}）")
        return 0
    print(f"レートを更新しました（{as_of}）")
    for code, value in sorted(rates.items()):
        print(f"  {code}  {value:>10,.2f} 円")
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
    p_db.add_argument(
        "db_action",
        choices=["migrate", "status", "check", "transfer", "backfill-values"],
    )
    p_db.add_argument(
        "--db",
        default=None,
        help="migrate/status/check では対象DB。transfer では移行元のSQLite（既定は config の db_path）",
    )
    p_db.set_defaults(func=_cmd_db)

    p_collect = sub.add_parser("collect", help="ソースから収集（経路A / 経路B）")
    p_collect.add_argument(
        "--source", required=True, help="editorial_sources または listing_sources の key"
    )
    p_collect.add_argument("--limit", type=int, default=None)
    p_collect.add_argument("--dry-run", action="store_true")
    p_collect.add_argument(
        "--explain", action="store_true", help="閾値未満も含めて判定内訳を表示（調整用）"
    )
    p_collect.add_argument(
        "--no-backfill", action="store_true",
        help="一覧ページからの過去記事の拾い直しをしない（フィードだけ見る）",
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
    group.add_argument(
        "--no-photo",
        action="store_true",
        help="代表画像が無い／単色のプレースホルダの候補（全ソース横断）",
    )
    p_remove.add_argument(
        "--dry-run", action="store_true", help="消さずに対象を一覧表示する"
    )
    p_remove.set_defaults(func=_cmd_remove)

    p_reset_img = sub.add_parser(
        "reset-images", help="取得済み画像を捨てて取り直す（抽出ルールを直したとき）"
    )
    group_img = p_reset_img.add_mutually_exclusive_group(required=True)
    group_img.add_argument("--id", type=int, help="property_id")
    group_img.add_argument(
        "--stale-skips",
        action="store_true",
        help="最小サイズ・許可形式を変えたあとに、その判定で弾いた記録だけ全件消す",
    )
    p_reset_img.set_defaults(func=_cmd_reset_images)

    p_img_report = sub.add_parser(
        "image-report", help="1物件の画像の内訳（採用・弾いた理由）を出す"
    )
    group_report = p_img_report.add_mutually_exclusive_group(required=True)
    group_report.add_argument("--id", type=int, help="property_id")
    group_report.add_argument(
        "--post-id", type=int, help="post_id（post show の先頭の番号）",
    )
    p_img_report.set_defaults(func=_cmd_image_report)

    p_refetch = sub.add_parser(
        "refetch-images",
        help="掲載ページを読み直して足りない画像を足す（既存は消さない・Driveは触らない）",
    )
    group_refetch = p_refetch.add_mutually_exclusive_group(required=True)
    group_refetch.add_argument("--id", type=int, help="property_id（1件だけ）")
    group_refetch.add_argument(
        "--source", help="ソースのkey。上限に届いていない物件をまとめて直す",
    )
    group_refetch.add_argument(
        "--all", dest="source", action="store_const", const=None,
        help="ソースを問わず、上限に届いていない物件をまとめて直す",
    )
    p_refetch.add_argument("--limit", type=int, help="対象の上限（件数）")
    p_refetch.add_argument("--dry-run", action="store_true", help="対象を並べるだけ")
    p_refetch.set_defaults(func=_cmd_refetch_images)

    p_ig = sub.add_parser("instagram", help="Instagram のトークン管理（[8] 自動投稿の土台）")
    p_ig.add_argument(
        "ig_action",
        choices=["auth-url", "exchange-code", "set-token", "check", "refresh",
                 "media", "fetch-media"],
        help=(
            "auth-url: 管理者に送る認可URLを出す / exchange-code: 受け取った code を"
            "トークンに換える / set-token: ダッシュボードで生成したトークンを直接貼る"
            " / media: **自分の**過去投稿を並べる / fetch-media: その画像を保存する"
        ),
    )
    p_ig.add_argument("--limit", type=int, default=25, help="media で並べる件数")
    p_ig.add_argument(
        "--id", action="append", default=[],
        help="fetch-media で取る投稿のID。**渡した順に 01- から番号が付く**",
    )
    p_ig.add_argument("--out", default="frames", help="fetch-media の保存先")
    p_ig.add_argument(
        "--redirect-uri",
        default="https://freming-curated-review.onrender.com/ig/callback",
        help="Meta の Business login settings に登録したものと完全に一致させること",
    )
    p_ig.set_defaults(func=_cmd_instagram)

    p_fx = sub.add_parser("fx", help="円換算レート（価格の並べ替えに使う）")
    p_fx.add_argument("fx_action", choices=["update", "show"])
    p_fx.add_argument(
        "--force", action="store_true", help="7日経っていなくても取得し直す"
    )
    p_fx.set_defaults(func=_cmd_fx)

    p_reel = sub.add_parser("reel", help="正方形の画像から週次リールを作る（[9]）")
    p_reel.add_argument(
        "reel_action", choices=["build", "preview", "publish", "tracks"],
        help=("build: 渡した画像で動画を作る / "
              "preview: **今週の自動リールを、出さずに1本組む** / "
              "publish: 作って**そのまま出す** / "
              "tracks: 週ごとの音源の並びを表示"),
    )
    p_reel.add_argument("images", nargs="*", help="正方形画像のパス（投稿順）")
    p_reel.add_argument("--out", default="reel.mp4", help="書き出し先")
    p_reel.add_argument(
        "--week", type=int, default=0,
        help="週番号。音源はこの剰余で選ぶので、同じ週なら何度作っても同じ曲になる",
    )
    p_reel.add_argument(
        "--post-id", type=int,
        help="publish で紐づける予定の行（post show の reel の番号）。"
             "付けないと自動のリールが同じ週にもう1本出る",
    )
    p_reel.add_argument(
        "--dry-run", action="store_true",
        help="publish で、動画と本文だけ作って投稿しない",
    )
    p_reel.add_argument(
        "--fallback", action="store_true",
        help="preview で、リーチが読めないときに直近の投稿で代用する"
             "（**本番の選び方とは変わる。** 仕上がりを見るためだけに使う）",
    )
    p_reel.set_defaults(func=_cmd_reel)

    p_post = sub.add_parser("post", help="Instagram への投稿（[9]）")
    p_post.add_argument(
        "post_action",
        choices=["plan", "show", "run", "replan", "reschedule", "requeue",
                 "skip", "unskip", "compact"],
        help=(
            "plan: 予定を作る / show: 予定を見る / run: 時間が来たものを投稿する"
            " / replan: まだ出していない予定の本文を作り直す"
            " / reschedule: 時刻を過ぎたままの予定を先送りする"
            " / requeue: IG側で消した投稿を予定に戻す（--id）"
            " / skip: 見送りにする（--id） / unskip: 見送りを戻す（--id）"
            " / compact: 空いた枠を詰める"
        ),
    )
    p_post.add_argument(
        "--id", type=int,
        help="requeue / skip / unskip で使う投稿のID（post show の先頭の番号）",
    )
    p_post.add_argument(
        "--now", action="store_true",
        help="requeue で「次の空き枠」ではなく今すぐ出す列に戻す",
    )
    p_post.add_argument(
        "--days", type=int,
        help="show で何日先まで出すか。既定は config の plan_days",
    )
    p_post.add_argument(
        "--limit", type=int,
        help="1回に投稿する件数の上限。**最初の1本は 1 にして様子を見ること**",
    )
    p_post.add_argument(
        "--dry-run", action="store_true",
        help="投稿せず、何が出るかだけ表示する（予定は消費しない）",
    )
    p_post.add_argument(
        "--kind", action="append", choices=["feed", "story", "reel"],
        help="扱う種別。既定は config の worker_kinds。複数指定できる",
    )
    p_post.set_defaults(func=_cmd_post)

    p_redeliver = sub.add_parser(
        "redeliver", help="納品記録を取り消して再納品させる（Driveのフォルダは消さない）"
    )
    p_redeliver.add_argument("property_id", type=int)
    p_redeliver.add_argument("--yes", action="store_true", help="確認せず実行する")
    p_redeliver.set_defaults(func=_cmd_redeliver)

    p_rescore = sub.add_parser(
        "rescore", help="採点し直す（基準を変えたあと既存分に当てる。API費用がかかる）"
    )
    p_rescore.add_argument(
        "--before", help="この日時より前に採点したものだけ（ISO。例 2026-08-07）"
    )
    p_rescore.add_argument("--limit", type=int, help="対象の上限")
    p_rescore.add_argument("--yes", action="store_true", help="確認せず実行する")
    p_rescore.set_defaults(func=_cmd_rescore)

    p_backfill = sub.add_parser(
        "backfill-captions",
        help="投稿本文で使う項目（用途・構造・面積・様式・英文・写真クレジット）を既存分に埋める",
    )
    p_backfill.add_argument("--limit", type=int, help="対象の上限")
    p_backfill.add_argument("--yes", action="store_true", help="確認せず実行する")
    p_backfill.set_defaults(func=_cmd_backfill_captions)

    p_relink = sub.add_parser(
        "backfill-listings",
        help="記事の中にある販売ページのURLを既存分に入れる（API費用なし）",
    )
    p_relink.add_argument("--limit", type=int, help="対象の上限")
    p_relink.add_argument("--yes", action="store_true", help="確認せず実行する")
    p_relink.set_defaults(func=_cmd_backfill_listings)

    p_sources_report = sub.add_parser(
        "source-report", help="ソース別の実績（収集→承認の歩留まり）"
    )
    p_sources_report.set_defaults(func=_cmd_sources_report)

    p_status = sub.add_parser("status", help="候補の件数をステータス別に表示")
    p_status.set_defaults(func=_cmd_status)

    return parser


# DBの列が揃っていないと動かないコマンド。列が足りないまま実行すると
# 「no such column」で落ち、原因が分かりにくいので手前で止める。
_NEEDS_MIGRATED_DB = frozenset({
    "collect", "score", "serve", "deliver", "learn", "rules",
    "ingest-url", "add-manual", "remove", "reset-images", "status",
    "instagram", "post", "redeliver", "rescore", "source-report",
    "backfill-captions", "backfill-listings", "image-report", "refetch-images",
    # reel は入れない。**build と tracks はDBを見ない**（渡した画像と
    # assets だけで動く）ので、DBの無い環境でも使えるようにしておく。
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
