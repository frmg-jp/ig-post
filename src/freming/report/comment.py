"""[10] 週次レポートの講評——**渡した数字だけで書かせる**。

    週の数字を組む → Claude に渡す → 数字を検算して保存

機械的な比較（weekly.py の考察）は「AとBで平均がこれだけ違った」まで
しか言えない。読み物としての講評——今週はこういう週だった、次はここを
試す価値がある——は文章でしか書けないので、週に1回だけ書かせる。

**危険は1つだけ。数字に無い話を書くこと。** 「先週より伸びた」「この
建築家は人気がある」のような、渡していない事実を作られると、次の選定が
嘘を根拠に動く。そこで:

  - 渡すのは**数字だけ**。記事の本文も画像も渡さない
  - プロンプトで「渡した数字以外の事実を書かない」と縛る
  - **出てきた数字を検算する。** 本文に出る数値が、渡した材料に無ければ
    捨てる（保存しない）。言い回しの縛りより、こちらが効く
  - 週に1回・1回だけ。画面を開くたびには呼ばない（費用と速度）

費用は Haiku で1回あたり1円に満たない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from freming.config import Config
from freming.db.connection import DbConnection
from freming.logging_setup import get_logger
from freming.report.weekly import WeeklyReport

log = get_logger(__name__)

MAX_TOKENS = 700

SYSTEM = """あなたは FREMING CURATED（世界の建築・不動産を紹介する
Instagram メディア）の運用担当です。週次の数字を受け取り、担当者に向けて
短い講評を書きます。

守ること:

- **渡された数字だけを材料にする。** それ以外の事実を書かない。
  世間の流行、他アカウントの動向、季節、アルゴリズムの推測は書かない。
- **因果を断定しない。** 「Aだから伸びた」ではなく「Aのほうが平均が
  高かった」と書く。理由は分からない。
- 本数が少ないときは、少ないと書く。無理に傾向を見つけない。
- リーチは時間とともに伸びる。出したばかりの投稿が低いのは当然で、
  それを「弱かった」と書かない。
- 日本語。3〜5文、300字以内。見出しも箇条書きも使わず、地の文で書く。
- 最後に「次に試すこと」を1つだけ、具体的に書く。在庫が無いものは
  勧めない。"""


@dataclass
class Comment:
    body: str
    source: str
    model: str


def _fmt(value: float) -> str:
    return f"{value:.0f}"


def build_source(report: WeeklyReport) -> str:
    """モデルに渡す材料。**ここに無い数字は本文に出てはいけない。**"""
    lines = [
        f"週: {report.label}",
        f"出した本数: {len(report.published)}（先週 {report.prev_count}）",
        f"リーチ合計: {report.reach_total}（先週 {report.prev_total}）",
        f"1本あたり: {_fmt(report.reach_avg)}（先週 {_fmt(report.prev_avg)}）",
        f"リーチが取れている本数: {report.measured}",
    ]
    if report.best is not None:
        from freming.report.weekly import name_of

        lines.append(f"いちばん見られた投稿: {name_of(report.best)}"
                     f"（リーチ {report.best['reach']}）")

    lines.append("")
    lines.append("今週出したもの:")
    for row in report.published:
        from freming.report.weekly import name_of

        reach = row["reach"] if row["reach"] is not None else "未取得"
        parts = [f"- {name_of(row)}: リーチ {reach}"]
        if row["property_id"]:
            parts.append(f"点数 {_fmt(float(row['score'] or 0))}")
            if row["genre"]:
                parts.append(str(row["genre"]))
            parts.append(f"写真 {row['image_count'] or 0}枚")
        lines.append("、".join(parts))

    if report.genres:
        lines.append("")
        lines.append("ジャンル別の平均リーチ（直近8週）:")
        for stat in report.genres:
            lines.append(f"- {stat.label}: {_fmt(stat.reach_avg)}（{stat.posts}本）")

    if report.insights:
        lines.append("")
        lines.append("機械的に出した比較:")
        for insight in report.insights:
            lines.append(f"- {insight.headline}（{insight.evidence}）")

    lines.append("")
    lines.append(f"未審査の在庫: {report.pending} 件 / "
                 f"投稿に回せる在庫: {report.approved_waiting} 件")
    return "\n".join(lines)


# 本文から拾う数値。「3本」「168」「1.5倍」など。
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def numbers_in(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in _NUMBER.finditer(text)}


def unsupported_numbers(body: str, source: str) -> set[str]:
    """**渡していない数字**を本文から拾う。空でなければ捨てる。

    言い回しで縛るより確実な検算。モデルが「先週より30%増」のような
    計算を勝手に始めたときも、その 30 が材料に無ければここで止まる。

    1桁の数（1〜9）は「3文で」「1つ」のような言い回しにも出るので
    見逃す。材料の数字はたいてい2桁以上で、取り違えが問題になるのも
    そちら。
    """
    allowed = numbers_in(source)
    return {n for n in numbers_in(body) - allowed if len(n.split(".")[0]) >= 2}


def write(config: Config, report: WeeklyReport) -> Comment:
    """講評を1本書かせる。**検算に落ちたら例外。**"""
    import anthropic

    source = build_source(report)
    model = config.scoring.model
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": source}],
    )
    body = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            body = block.text.strip()
            break
    if not body:
        raise RuntimeError("講評が空でした")

    stray = unsupported_numbers(body, source)
    if stray:
        # **黙って直さない。** 数字を書き換えると、何が起きたのか
        # あとから分からなくなる。捨てて、理由を出す。
        raise RuntimeError(
            "渡していない数字が本文に出ました: "
            + "、".join(sorted(stray))
            + "。保存しません。"
        )
    return Comment(body=body, source=source, model=model)


# ----------------------------------------------------------------------
# 保存と読み出し
# ----------------------------------------------------------------------
def save(conn: DbConnection, week_start: str, comment: Comment) -> None:
    conn.execute("DELETE FROM weekly_notes WHERE week_start = ?", (week_start,))
    conn.execute(
        "INSERT INTO weekly_notes (week_start, body, source, model, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (week_start, comment.body, comment.source, comment.model,
         datetime.now(UTC).isoformat()),
    )
    conn.commit()


def load(conn: DbConnection, week_start: str):
    """その週の講評。無ければ None。**画面はこれを読むだけ。**"""
    try:
        return conn.execute(
            "SELECT * FROM weekly_notes WHERE week_start = ?", (week_start,)
        ).fetchone()
    except Exception:  # noqa: BLE001 - 列が無い環境でも画面は出す
        return None


__all__ = [
    "MAX_TOKENS",
    "SYSTEM",
    "Comment",
    "build_source",
    "load",
    "numbers_in",
    "save",
    "unsupported_numbers",
    "write",
]
