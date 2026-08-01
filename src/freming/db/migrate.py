"""マイグレーション適用。

migrations/NNNN_name.sql を番号順に、未適用のものだけトランザクションで
適用する。適用済みは schema_migrations に記録するので再実行は安全。

単体実行:
    python -m freming.db.migrate [--db data/freming.db] [--status]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from freming.config import load_config
from freming.db.connection import connect
from freming.logging_setup import get_logger, setup_logging

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """migrations ディレクトリの .sql をファイル名順に返す。"""
    if not directory.exists():
        raise FileNotFoundError(f"マイグレーションディレクトリがありません: {directory}")
    return [Migration(version=p.stem, path=p) for p in sorted(directory.glob("*.sql"))]


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    conn.execute(_TRACKING_TABLE)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def migrate(db_path: str | Path) -> list[str]:
    """未適用のマイグレーションを適用し、適用したバージョンを返す。"""
    conn = connect(db_path)
    newly_applied: list[str] = []
    try:
        done = applied_versions(conn)
        for migration in discover_migrations():
            if migration.version in done:
                log.debug("適用済みのためスキップ: %s", migration.version)
                continue
            log.info("マイグレーション適用: %s", migration.version)
            try:
                conn.execute("BEGIN")
                conn.executescript(migration.sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)", (migration.version,)
                )
                conn.commit()
            except Exception:
                conn.rollback()
                log.exception("マイグレーション失敗: %s", migration.version)
                raise
            newly_applied.append(migration.version)
        if not newly_applied:
            log.info("適用すべきマイグレーションはありません（最新）")
        else:
            log.info("%d 件のマイグレーションを適用しました", len(newly_applied))
        return newly_applied
    finally:
        conn.close()


def status(db_path: str | Path) -> list[tuple[str, bool]]:
    """(version, applied) の一覧を返す。"""
    conn = connect(db_path)
    try:
        done = applied_versions(conn)
        return [(m.version, m.version in done) for m in discover_migrations()]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DBマイグレーションの適用")
    parser.add_argument("--db", default=None, help="DBパス（省略時は config.yaml の app.db_path）")
    parser.add_argument("--status", action="store_true", help="適用状況の表示のみ")
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    db_path = args.db or cfg.app.db_path

    if args.status:
        for version, is_applied in status(db_path):
            print(f"{'[x]' if is_applied else '[ ]'} {version}")
        return 0

    migrate(db_path)
    print(f"OK: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
