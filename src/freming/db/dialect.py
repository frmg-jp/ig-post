"""SQLite と PostgreSQL の差を1か所に閉じ込める。

なぜ両対応にするか:

  - 定期実行（GitHub Actions）と審査UIが同じDBを見る必要があるため、
    本番は PostgreSQL（Supabase）。
  - ただしテストは DBサーバーなしで動かしたい。SQLite のままにしておけば
    テストが速く、CI にサービスコンテナが要らない。

方針は「SQL は SQLite の書き方で書き、ここで PostgreSQL に寄せる」。
差が増えたらこのモジュールにルールを足す。呼び出し側には持ち込まない。

両方で動くように、SQL を書くときは次を守る:

  - プレースホルダは ? （ここで %s に変換する）
  - INSERT ... ON CONFLICT DO NOTHING RETURNING id （OR IGNORE と
    lastrowid は使わない。PostgreSQL に無い）
  - 時刻の比較に datetime('now', ...) を使わない。Python 側で計算して
    パラメータで渡す（書式の食い違いを避ける）
"""

from __future__ import annotations

import re
from pathlib import Path

SQLITE = "sqlite"
POSTGRES = "postgres"

# PostgreSQL の接続文字列。Supabase は postgresql:// を配る。
_POSTGRES_SCHEMES = ("postgres://", "postgresql://")


def dialect_of(target: str | Path) -> str:
    """接続先の文字列から方言を決める。"""
    text = str(target)
    if text.startswith(_POSTGRES_SCHEMES):
        return POSTGRES
    return SQLITE


# ----------------------------------------------------------------------
# プレースホルダ
# ----------------------------------------------------------------------
def to_paramstyle(sql: str, dialect: str) -> str:
    """? を %s に変換する（PostgreSQL のみ）。

    文字列リテラルの中の ? は変換しない。LIKE のパターンなどに ? が
    入っていたときに壊さないため。あわせて、元から入っている % は
    psycopg がプレースホルダと誤認するので %% にエスケープする。
    """
    if dialect != POSTGRES:
        return sql

    out: list[str] = []
    quote: str | None = None
    for char in sql:
        # % は文字列リテラルの中でもエスケープが要る。psycopg はクエリ全体を
        # %-フォーマットで展開するため、リテラル内の % も置換対象になる。
        if char == "%":
            out.append("%%")
            continue
        if quote is not None:
            out.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            # ? はリテラル内では素通し（LIKE のパターンなどを壊さない）
            out.append("%s")
        else:
            out.append(char)
    return "".join(out)


# ----------------------------------------------------------------------
# DDL
# ----------------------------------------------------------------------
# SQLite の `id INTEGER PRIMARY KEY` は暗黙に採番されるが、PostgreSQL では
# 採番されない。移行時にここだけが実質的な差だったので、置換1つで済ませる。
# 差が増えたら NNNN_name.postgres.sql を置けば、そちらが優先される。
_IMPLICIT_PK = re.compile(r"^(\s*)id INTEGER PRIMARY KEY\b", re.MULTILINE)


def to_postgres_ddl(sql: str) -> str:
    return _IMPLICIT_PK.sub(
        r"\1id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY", sql
    )


def translate_ddl(sql: str, dialect: str) -> str:
    return to_postgres_ddl(sql) if dialect == POSTGRES else sql


def redact(target: str | Path) -> str:
    """ログに出せる形にする。接続文字列にはパスワードが入っている。"""
    text = str(target)
    if not text.startswith(_POSTGRES_SCHEMES):
        return text
    # postgresql://user:pass@host/db → postgresql://user:***@host/db
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", text)
