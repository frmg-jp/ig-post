"""マイグレーション適用。

migrations/NNNN_name.sql を番号順に、未適用のものだけトランザクションで
適用する。適用済みは schema_migrations に記録するので再実行は安全。

単体実行:
    python -m freming.db.migrate [--db data/freming.db] [--status]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from freming.config import load_config
from freming.db.connection import SQLITE, DbConnection, connect, dialect_for
from freming.db.dialect import translate_ddl
from freming.logging_setup import get_logger, setup_logging

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def sql_for(self, dialect: str) -> str:
        """方言ごとのSQL。

        NNNN_name.postgres.sql を置けばそちらが優先される（逃げ道）。
        いまのところ差は `id INTEGER PRIMARY KEY` の採番だけなので、
        置換で足りている。
        """
        override = self.path.with_suffix(f".{dialect}.sql")
        if override.exists():
            return override.read_text(encoding="utf-8")
        return translate_ddl(self.sql, dialect)


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """migrations ディレクトリの .sql をファイル名順に返す。

    NNNN_name.postgres.sql のような方言別ファイルは、それ自体を
    マイグレーションとしては数えない（本体から参照される）。
    """
    if not directory.exists():
        raise FileNotFoundError(f"マイグレーションディレクトリがありません: {directory}")
    return [
        Migration(version=p.stem, path=p)
        for p in sorted(directory.glob("*.sql"))
        if "." not in p.stem
    ]


def applied_versions(conn: DbConnection) -> set[str]:
    conn.execute(_TRACKING_TABLE)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def _run_script(conn: DbConnection, sql: str) -> None:
    """複数文のSQLを流す。

    sqlite3 の executescript は暗黙にコミットしてしまい、PostgreSQL には
    そもそも無い。どちらでも同じ意味になるよう、文単位で実行する。
    """
    for statement in _split_statements(sql):
        conn.execute(statement)


def _split_statements(sql: str) -> list[str]:
    """; 区切りで文に分ける。行コメントは落とす。

    マイグレーションのSQLは自分たちで書いたものだけなので、
    文字列リテラル中の ; までは考慮しない（現に1つも無い）。
    """
    lines = [
        line for line in sql.splitlines()
        if not line.strip().startswith("--")
    ]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def migrate(db_path: str | Path) -> list[str]:
    """未適用のマイグレーションを適用し、適用したバージョンを返す。"""
    dialect = dialect_for(db_path)
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
                _run_script(conn, migration.sql_for(dialect))
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


class PendingMigrations(RuntimeError):
    """未適用のマイグレーションがある。DBの列が足りない状態で動かさない。"""


def ensure_migrated(db_path: str | Path) -> None:
    """未適用のマイグレーションがあれば、動き出す前に止める。

    列が足りないまま起動すると「no such column」で全画面が500になり、
    原因が分かりにくい。先に何をすればよいかを出して止める。
    """
    # SQLite はファイルが無ければ「まだ何もしていない」と分かる。
    # PostgreSQL は接続してみないと分からないので、status() に任せる。
    if dialect_for(db_path) == SQLITE and not Path(db_path).exists():
        raise PendingMigrations(
            f"DBがまだありません（{db_path}）。次を実行してください:\n"
            "  python -m freming.cli db migrate"
        )
    pending = [version for version, applied in status(db_path) if not applied]
    if pending:
        raise PendingMigrations(
            f"未適用のマイグレーションが {len(pending)} 件あります: {', '.join(pending)}\n"
            "次を実行してください:\n"
            "  python -m freming.cli db migrate"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DBマイグレーションの適用")
    parser.add_argument("--db", default=None, help="DBパス/接続文字列（省略時は DATABASE_URL か config.yaml の app.db_path）")
    parser.add_argument("--status", action="store_true", help="適用状況の表示のみ")
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg.app.log_dir, cfg.app.log_level)
    db_path = args.db or cfg.app.target()

    if args.status:
        for version, is_applied in status(db_path):
            print(f"{'[x]' if is_applied else '[ ]'} {version}")
        return 0

    migrate(db_path)
    print(f"OK: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def is_missing_table(exc: BaseException) -> bool:
    """「そのテーブル（列）はまだ無い」という失敗か。

    マイグレーションを流す場所（定期実行）と、コードが配られる場所
    （Render の自動デプロイ）が別なので、**新しいコードが先に動き出す
    時間帯がある。** そのとき画面が500で落ちると、原因が分からない。
    ここで見分けて「db migrate を実行してください」と出す。

    方言ごとにメッセージが違うので、両方の言い回しを見る。
      SQLite   : no such table: posts / no such column: p.permalink
      PostgreSQL: relation "posts" does not exist / column ... does not exist
    """
    text = str(exc).lower()
    return (
        "no such table" in text
        or "no such column" in text
        or "does not exist" in text
        or "undefinedtable" in text
        or "undefinedcolumn" in text
    )
