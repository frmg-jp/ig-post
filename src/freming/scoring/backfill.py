"""[2] 既存の物件に、投稿本文で使う項目だけを埋める。

    Usage / Structure / 面積 / Style / 英文 / 写真クレジット

これらは 0012・0013 で足した列で、**それ以降に採点したものにしか入って
いない**。既に納品済みのものこそ投稿に回るので、そこを埋めないと
仕様欄が Location と Built in だけの本文になる。

## 採点し直すのとは分けてある

`rescore` は採点結果を捨てて採点し直す。基準（築年の足切りなど）を
変えたときに使うもので、**納品済みは対象にしない**（既に人が承認して
外に出したものを、あとからルールで落としても意味がない）。

こちらは逆に**納品済みも対象にする**。そのうえで、

  - score / score_reason / status には**一切触らない**
  - summary も上書きしない（審査で人が見た文言をそのまま残す）
  - 足した列だけを書く

過去の審査結果を動かさずに、本文の材料だけを増やす。

## 費用

1件につき記事1本をLLMに読ませるので、`rescore` と同じだけかかる。
何件をいくらで叩くのかを先に出し、`--yes` が無ければ何もしない。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from freming.config import Config, load_config
from freming.db.connection import DbConnection, Row, connect
from freming.logging_setup import get_logger, setup_logging
from freming.scoring.client import ScoringClient, ScoringError
from freming.scoring.prompt import build_system_prompt, build_user_prompt

log = get_logger(__name__)

# 埋める列。Assessment の属性名と同じ並びにしてある。
FIELDS = (
    "usage_type",
    "structure",
    "building_area",
    "site_area",
    "style_name",
    "summary_en",
    "photo_credit",
    # 0014 で足した、投稿の型（2026-08-19 の実運用4投稿）に要る3つ。
    "display_name",
    "caption_body",
    "location_region",
)


@dataclass
class BackfillStats:
    filled: int = 0
    empty: int = 0          # 記事に書いていなかった（呼び直しても埋まらない）
    failed: int = 0
    no_text: int = 0        # 本文が無いので読ませようがない
    lines: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"埋めました {self.filled} 件"
        parts = []
        if self.empty:
            parts.append(f"記事に記載なし {self.empty}")
        if self.failed:
            parts.append(f"失敗 {self.failed}")
        if self.no_text:
            parts.append(f"本文なし {self.no_text}")
        return f"{text}（{' / '.join(parts)}）" if parts else text


def pending_rows(conn: DbConnection, limit: int | None = None) -> list[Row]:
    """まだ埋まっていない物件。納品済みも含める。

    1つでも空いていれば対象にする。全部埋まっている行は呼ばない。
    """
    missing = " OR ".join(f"{f} IS NULL" for f in FIELDS)
    sql = (
        f"SELECT * FROM properties WHERE ({missing}) AND scored_at IS NOT NULL "
        "ORDER BY status = 'delivered' DESC, score DESC, id DESC"
    )
    params: tuple = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    return conn.execute(sql, params).fetchall()


def estimate(rows: list[Row]) -> tuple[int, int, float]:
    """(入力トークン, 出力トークン, 概算ドル)。単価は Haiku 4.5。

    出力は caption_body（250〜450字の日本語）が一番大きい。0014 で
    足したぶんを 400 → 800 に増やしてある。
    """
    chars = sum(len(r["content_text"] or "") for r in rows)
    tokens_in = int(chars / 2.5 + len(rows) * 1200)
    tokens_out = len(rows) * 800
    return tokens_in, tokens_out, tokens_in / 1e6 * 1.0 + tokens_out / 1e6 * 5.0


def save_fields(conn: DbConnection, property_id: int, values: dict[str, str]) -> int:
    """空いている列だけ埋める。**既に値があるものは上書きしない。**

    人が直した値を、あとからLLMの出力で潰さないため。
    """
    filled = {k: v for k, v in values.items() if v}
    if not filled:
        return 0
    sets = ", ".join(f"{k} = COALESCE({k}, ?)" for k in filled)
    conn.execute(
        f"UPDATE properties SET {sets} WHERE id = ?", (*filled.values(), property_id)
    )
    conn.commit()
    return len(filled)


def backfill(
    config: Config,
    conn: DbConnection,
    limit: int | None = None,
    client: ScoringClient | None = None,
) -> BackfillStats:
    """対象を順に読み直して、足した列だけ埋める。"""
    stats = BackfillStats()
    rows = pending_rows(conn, limit)
    if not rows:
        log.info("埋める対象はありません")
        return stats

    if client is None:
        # 採点し直すわけではないので、直近の不承認理由やルールは載せない。
        # 抽出の指示だけで足りるうえ、載せるとプロンプトが伸びて費用が増える。
        client = ScoringClient(config, build_system_prompt(config, [], []))

    for row in rows:
        if not (row["content_text"] or "").strip():
            stats.no_text += 1
            continue
        try:
            assessment = client.assess(build_user_prompt(row))
        except ScoringError as exc:
            log.error("読み直せませんでした: %s (%s)", row["source_url"], exc)
            stats.failed += 1
            continue

        values = {f: getattr(assessment, f, "") for f in FIELDS}
        count = save_fields(conn, int(row["id"]), values)
        if count:
            stats.filled += 1
            stats.lines.append(
                f"  {row['id']:>5}  {count}項目  {(row['title'] or '')[:44]}"
            )
        else:
            stats.empty += 1

    log.info(stats.summary())
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="投稿本文で使う項目を既存の物件に埋める（API費用がかかる）"
    )
    parser.add_argument("--limit", type=int, help="対象の上限")
    parser.add_argument("--yes", action="store_true", help="確認せず実行する")
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.app.log_dir, config.app.log_level)
    conn = connect(config.app.target())
    try:
        rows = pending_rows(conn, args.limit)
        if not rows:
            print("埋める対象はありません。")
            return 0
        tokens_in, tokens_out, cost = estimate(rows)
        delivered = sum(1 for r in rows if r["status"] == "delivered")
        print(f"対象 {len(rows)} 件（うち納品済み {delivered} 件）")
        print(f"  入力 約 {tokens_in / 1000:.0f}k / 出力 約 {tokens_out / 1000:.0f}k トークン")
        print(f"  概算費用 **約 ${cost:.2f}**（{config.scoring.model} の単価。目安です）")
        if not args.yes:
            print(
                "\nスコアと審査結果には触りません。足した列だけを埋めます。\n"
                "実行するには --yes を付けてもう一度。"
            )
            return 0
        stats = backfill(config, conn, args.limit)
    finally:
        conn.close()
    print(stats.summary())
    print("\n".join(stats.lines[:40]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
