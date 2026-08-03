"""SQLite の中身を PostgreSQL に移す（1回きり）。

移行時に一番危ないのは **deliveries を取りこぼすこと**。frmg_igNNN の
連番はこのテーブルの最大値から採っているので、移し忘れると frmg_ig001 から
振り直しになり、Drive 上で既存フォルダと衝突する。ここでは移行後に
必ず件数を突き合わせ、合わなければ失敗として扱う。

単体実行:
    python -m freming.db.transfer --from data/freming.db --to "$DATABASE_URL"
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from freming.db.connection import connect
from freming.db.dialect import POSTGRES, dialect_of, redact
from freming.db.migrate import migrate
from freming.logging_setup import get_logger, setup_logging

log = get_logger(__name__)

# 依存の順に並べる。properties が先でないと外部キーで落ちる。
TABLES = [
    "properties",
    "feedback",
    "deliveries",
    "images",
    "image_skips",
    "jobs",
    "rule_candidates",
]

# 移行後に採番の続きを合わせる必要がある列（PostgreSQL の IDENTITY）
_SEQUENCE_TABLES = TABLES


@dataclass
class TransferStats:
    copied: dict[str, int] = field(default_factory=dict)
    skipped_tables: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"  {name:<16} {count:>6} 行" for name, count in self.copied.items()]
        head = f"移行しました（{sum(self.copied.values())} 行）"
        if self.skipped_tables:
            head += f"／存在しなかったテーブル: {', '.join(self.skipped_tables)}"
        return "\n".join([head, *lines])


class TransferError(RuntimeError):
    """移行が完了しなかった。"""


def _columns(conn, table: str) -> list[str] | None:
    try:
        cursor = conn.execute(f"SELECT * FROM {table} LIMIT 0")
    except Exception:  # noqa: BLE001 - テーブルが無い場合
        return None
    return [d[0] for d in cursor.description]


def _count(conn, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"] if not isinstance(row, tuple) else row[0])


def transfer(source: str | Path, dest: str) -> TransferStats:
    """source（SQLite）の中身を dest（PostgreSQL）へ写す。

    dest 側は空であることを前提にする。既に行があるテーブルは
    二重投入を避けるため失敗させる。
    """
    if dialect_of(dest) != POSTGRES:
        raise TransferError(f"移行先が PostgreSQL ではありません: {redact(dest)}")

    log.info("移行先にマイグレーションを適用します: %s", redact(dest))
    migrate(dest)

    stats = TransferStats()
    src = connect(source)
    dst = connect(dest)
    try:
        for table in TABLES:
            columns = _columns(src, table)
            if columns is None:
                stats.skipped_tables.append(table)
                continue
            if _count(dst, table) > 0:
                raise TransferError(
                    f"移行先の {table} に既に行があります。"
                    "空のデータベースに対して実行してください（二重投入を防ぐため）。"
                )

            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                stats.copied[table] = 0
                continue

            placeholders = ", ".join("?" for _ in columns)
            sql = (
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"OVERRIDING SYSTEM VALUE VALUES ({placeholders})"
            )
            for row in rows:
                dst.execute(sql, tuple(row[c] for c in columns))
            stats.copied[table] = len(rows)
            log.info("%s: %d 行", table, len(rows))

        _resync_sequences(dst)
        dst.commit()
        _verify(src, dst, stats)
    except Exception:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()
    return stats


def _resync_sequences(dst) -> None:
    """IDENTITY の次の値を、入れた行の最大 id に合わせる。

    これをやらないと、移行後の最初の INSERT が id=1 を採ろうとして
    主キー衝突で落ちる。
    """
    for table in _SEQUENCE_TABLES:
        if _columns(dst, table) is None:
            continue
        dst.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
            f"(SELECT COUNT(*) FROM {table}) > 0)"
        )


def _verify(src, dst, stats: TransferStats) -> None:
    """件数を突き合わせる。

    deliveries が合わないのは特に危ない（frmg_igNNN の連番が振り直しになり、
    Drive 上で既存フォルダと衝突する）ので、メッセージでそう伝える。
    """
    for table, expected in stats.copied.items():
        actual = _count(dst, table)
        if actual != expected:
            extra = (
                "／frmg_igNNN の連番はこのテーブルの最大値から採るため、"
                "取りこぼすと採番が振り直しになり Drive 上で衝突します"
                if table == "deliveries" else ""
            )
            raise TransferError(
                f"{table} の件数が合いません（移行元 {expected} / 移行先 {actual}）{extra}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite の中身を PostgreSQL に移す")
    parser.add_argument("--from", dest="source", default="data/freming.db")
    parser.add_argument("--to", dest="dest", required=True, help="postgresql://...")
    args = parser.parse_args(argv)

    setup_logging(Path("logs"), "INFO")
    if not Path(args.source).exists():
        print(f"移行元が見つかりません: {args.source}", file=sys.stderr)
        return 2
    try:
        stats = transfer(args.source, args.dest)
    except TransferError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(stats.summary())
    print(
        "\n確認してください:\n"
        "  DATABASE_URL=... python -m freming.cli status\n"
        "  DATABASE_URL=... python -m freming.cli serve"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
