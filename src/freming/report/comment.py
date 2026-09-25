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
- **数字は渡されたものをそのまま書く。** 丸めない（87 を「90近く」と
  書かない）。位を落とさない（93 を「90点台」と書かない）。割り算や
  引き算をして新しい数字を作らない。幅で書かない（「7〜8本」と
  書かない）。自信が無ければ数字を書かずに、「増えた」「少なかった」と
  言葉で書く。
- **「今週」と書かない。** 講評を書くのは終わった週についてで、読む人は
  次の週の途中で読む。「この週は」と書く。
- 日本語。3〜5文、300字以内。見出しも箇条書きも使わず、地の文で書く。
- **記号で強調しない。** ** や __ や # は使わない。画面にはそのまま
  星印として出る（2026-09-26 に実際に出た）。強調したいことは語順で書く。
- 最後に「次に試すこと」を1つだけ、具体的に書く。在庫が無いものは
  勧めない。"""

# 検算に落ちたとき、1回だけ書き直させる。落ちた数字を名指しで返す。
# **甘くするわけではない。** 直したものも同じ検算にかけ、落ちれば捨てる。
RETRY = """その文には、渡していない数字が出ています: {stray}

渡した材料に無い数字は書けません。丸めた数（87 を「90近く」）や、
位を落とした数（93 を「90点台」）、こちらで計算した数（増減の割合）も
同じです。その数字を材料にあるものへ置き換えるか、数字を使わない
言い方に直して、講評だけをもう一度書いてください。"""


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
        # **未取得の本数も渡す。** 画面には出ているのに材料に無かったので、
        # 「3本が未取得」と書いた講評が検算で捨てられた（2026-09-16）。
        # 事実は正しいのに落ちる——材料の側の漏れだった。
        f"リーチが未取得の本数: {len(report.published) - report.measured}",
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

    # 振り返りのチェック（偏りなど）。画面に出ている事実なので、材料にも
    # 入れる。入れないと、正しいことを書いても検算で落ちる。
    flagged = [c for c in report.checks if c.note]
    if flagged:
        lines.append("")
        lines.append("気になった点:")
        for check in flagged:
            lines.append(f"- {check.label}: {check.note}")

    lines.append("")
    lines.append(f"未審査の在庫: {report.pending} 件 / "
                 f"投稿に回せる在庫: {report.approved_waiting} 件")
    return "\n".join(lines)


# 本文から拾う数値。「3本」「168」「1.5倍」など。
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

# **数の単位。** 1桁でもこれが付いていれば、材料の数字を指している。
# 「7〜8本」の 7 がこれで引っかかる（2026-09-16 に実際にすり抜けた）。
# 単位に届くまでに挟まるもの（幅の記号・区切り・別の数字）は読み飛ばす。
_COUNTER = re.compile(r"[〜～~\-–—ー、,.／/や0-9]*([本枚点件％%倍割])")


def numbers_in(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in _NUMBER.finditer(text)}


def _checked(body: str) -> set[str]:
    """検算にかける数字。**1桁は単位が付いているときだけ見る。**

    1桁の数は「次に試すことを1つ」「3文で」のような言い回しにも出るので、
    全部見ると通らない。ただし「8本」「3枚」は材料の数字を指しているので、
    単位が続いていれば1桁でも見る。幅で書かれた「7〜8本」の 7 も、
    単位まで読み飛ばして拾う。
    """
    out: set[str] = set()
    for match in _NUMBER.finditer(body):
        value = match.group(0).replace(",", "")
        if len(value.split(".")[0]) >= 2 or _COUNTER.match(body, match.end()):
            out.add(value)
    return out


def unsupported_numbers(body: str, source: str) -> set[str]:
    """**渡していない数字**を本文から拾う。空でなければ捨てる。

    言い回しで縛るより確実な検算。モデルが「先週より30%増」のような
    計算を勝手に始めたときも、その 30 が材料に無ければここで止まる。
    """
    return _checked(body) - numbers_in(source)


def plain(text: str) -> str:
    """マークダウンの強調記号を落とす。**画面にそのまま出るため。**

    プロンプトで「使うな」と書いてあるが、守られないことがある
    （2026-09-26 に `**米国以外の国で…**` がそのまま保存された）。
    言い回しの縛りだけに頼らない——検算と同じ考え方で、出たものを直す。

    消すのは強調の記号だけ。**数字も語も変えない**（検算の前に通すので、
    ここで中身を書き換えると検算が意味を失う）。
    """
    out = text.replace("**", "").replace("__", "")
    lines = [line.lstrip("#").lstrip() if line.lstrip().startswith("#") else line
             for line in out.splitlines()]
    return "\n".join(lines).strip()


def _text(response) -> str:
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return plain(block.text)
    return ""


def write(config: Config, report: WeeklyReport) -> Comment:
    """講評を1本書かせる。**検算に落ちたら、1回だけ書き直させて、それでも
    落ちたら例外。**

    初回に落ちるのは、たいてい丸め（87 を「90近く」）か位落とし（93 を
    「90点台」）で、書き直させれば通る。2026-09-16 の初回がこれだった。
    **検算を緩めることはしない。** 直したものも同じ検算にかける。
    """
    import anthropic

    source = build_source(report)
    model = config.scoring.model
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    messages: list[dict] = [{"role": "user", "content": source}]

    body = ""
    stray: set[str] = set()
    for attempt in range(2):
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            messages=messages,
        )
        body = _text(response)
        if not body:
            raise RuntimeError("講評が空でした")

        stray = unsupported_numbers(body, source)
        if not stray:
            return Comment(body=body, source=source, model=model)
        if attempt == 0:
            log.warning("講評に渡していない数字が出たので書き直させます: %s",
                        "、".join(sorted(stray)))
            messages.append({"role": "assistant", "content": body})
            messages.append({
                "role": "user",
                "content": RETRY.format(stray="、".join(sorted(stray))),
            })

    # **黙って直さない。** 数字を書き換えると、何が起きたのか
    # あとから分からなくなる。捨てて、理由を出す。
    raise RuntimeError(
        "渡していない数字が本文に出ました: "
        + "、".join(sorted(stray))
        + "。書き直させても直らなかったので保存しません。"
    )


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


def delete(conn: DbConnection, week_start: str) -> bool:
    """その週の講評を消す。**読み間違いを書いたものを残さないため。**"""
    if load(conn, week_start) is None:
        return False
    conn.execute("DELETE FROM weekly_notes WHERE week_start = ?", (week_start,))
    conn.commit()
    return True


def load(conn: DbConnection, week_start: str):
    """その週の講評。無ければ None。**画面はこれを読むだけ。**"""
    try:
        return conn.execute(
            "SELECT * FROM weekly_notes WHERE week_start = ?", (week_start,)
        ).fetchone()
    except Exception:  # noqa: BLE001 - 列が無い環境でも画面は出す
        return None


def load_latest(conn: DbConnection, *, not_after: str | None = None):
    """**いちばん新しい講評。** 無ければ None。

    講評は終わった週について書く（月曜の定期実行）ので、いま開いている
    週にはまだ無い。「今週はまだ途中です」と空欄を出すより、先週書いた
    ものを週の名前つきで出したほうが読める。

    not_after を渡すと、その週より後のものは返さない（過去の週を
    ?week= で見ているときに、未来の講評を出さないため）。
    """
    sql = "SELECT * FROM weekly_notes"
    params: tuple = ()
    if not_after is not None:
        sql += " WHERE week_start <= ?"
        params = (not_after,)
    sql += " ORDER BY week_start DESC LIMIT 1"
    try:
        return conn.execute(sql, params).fetchone()
    except Exception:  # noqa: BLE001 - 列が無い環境でも画面は出す
        return None


__all__ = [
    "MAX_TOKENS",
    "RETRY",
    "SYSTEM",
    "Comment",
    "build_source",
    "delete",
    "load",
    "load_latest",
    "numbers_in",
    "plain",
    "save",
    "unsupported_numbers",
    "write",
]
