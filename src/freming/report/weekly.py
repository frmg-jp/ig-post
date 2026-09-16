"""[10] 週次レポート——「今週の10件」と Pick of the Week。

FREMING CURATED の編集方針（WEEKLY GLOBAL ARCHITECTURE REPORT）を、
**手元にある候補で**組み立てる。外へは一切出ない（DBを読むだけ）ので
費用はかからず、何度開いても同じ。

作る中身:

  - **今週の候補**: 未審査のうち点数の高い順。ジャンル別に見出しを立てる
    （Conversion / Hidden Gem / Architect …）。編集方針の「カテゴリーが
    偏っていないか」を目で確かめるため
  - **Pick of the Week**: いちばん点の高い1件。「飛行機に乗ってでも見に
    行く1軒」を人が選び直せるよう、**候補の並びも一緒に出す**
  - **今週出したもの**: 公開済みの投稿とリーチ
  - **最終チェック**: 10件あるか / 米国に偏っていないか / Hidden Gem と
    Conversion が入っているか。編集方針の最後の確認欄に対応する

**これは調査の代わりにはならない。** 編集方針が挙げている Wallpaper* や
The Modern House を読んで世界から探す仕事は、収集ソースを増やす話で、
この画面では増えない。ここに出るのは「いま手元にある候補」だけ。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from freming.config import Config
from freming.db.connection import DbConnection, Row

# 編集方針の目安。1週間に選ぶ件数。
TARGET_COUNT = 10

# 見出しに使う日本語。genre の値は scoring/schema.py の GENRES。
GENRE_LABELS = {
    "architect": "Architect-Designed",
    "adaptive_reuse": "Conversion（用途変更）",
    "hidden_gem": "Hidden Gem",
    "penthouse": "Penthouse",
    "loft": "Loft",
    "unknown": "その他",
}


@dataclass
class Section:
    genre: str
    label: str
    rows: list[Row]


@dataclass
class Check:
    """編集方針の最終チェック欄。1行ずつ ○／要確認 を出す。"""

    label: str
    ok: bool
    note: str = ""


@dataclass
class WeeklyReport:
    start: datetime          # 週の初日（現地時刻）
    end: datetime            # 週の最終日の翌日0時（現地時刻）
    generated_at: datetime
    candidates: list[Row] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    pick: Row | None = None
    published: list[Row] = field(default_factory=list)
    collected: int = 0
    approved: int = 0
    rejected: int = 0
    by_country: Counter = field(default_factory=Counter)
    by_genre: Counter = field(default_factory=Counter)
    checks: list[Check] = field(default_factory=list)

    @property
    def label(self) -> str:
        last = self.end - timedelta(days=1)
        return f"{self.start:%Y/%m/%d}（月）〜 {last:%m/%d}（日）"

    @property
    def reach_total(self) -> int:
        return sum(int(row["reach"] or 0) for row in self.published)


def week_bounds(config: Config, now: datetime) -> tuple[datetime, datetime]:
    """その日を含む週の月曜0時と、翌週の月曜0時（現地時刻）。"""
    zone = ZoneInfo(config.instagram.timezone)
    local = now.astimezone(zone)
    monday = datetime.combine(
        local.date() - timedelta(days=local.weekday()), time.min, tzinfo=zone
    )
    return monday, monday + timedelta(days=7)


def _flag(row: Row, key: str) -> bool:
    try:
        return bool(row[key])
    except (KeyError, IndexError, TypeError):
        return False


def axes_of(row: Row) -> list[tuple[str, float, str]]:
    """採点の軸を (名前, 点, 理由) で返す。score_detail が無ければ空。"""
    try:
        detail = row["score_detail"]
    except (KeyError, IndexError, TypeError):
        return []
    if not detail:
        return []
    try:
        parsed = json.loads(detail)
    except (ValueError, TypeError):
        return []
    return [
        (str(axis.get("key") or ""), float(axis.get("raw") or 0), str(axis.get("reason") or ""))
        for axis in parsed.get("axes") or []
    ]


def build(
    config: Config, conn: DbConnection, now: datetime | None = None,
    *, limit: int = TARGET_COUNT,
) -> WeeklyReport:
    """レポートを組み立てる。**DBを読むだけ。**"""
    now = now or datetime.now(UTC)
    start, end = week_bounds(config, now)
    report = WeeklyReport(
        start=start, end=end,
        generated_at=now.astimezone(ZoneInfo(config.instagram.timezone)),
    )

    # 今週の候補。**収集した日では絞らない。** 先週入った物件が今週の
    # いちばん良い1軒であることは普通にある。編集方針も「その週に本当に
    # 面白いものがあれば」と言っていて、入荷日の話はしていない。
    report.candidates = conn.execute(
        "SELECT * FROM properties WHERE status = 'pending' AND score IS NOT NULL "
        "ORDER BY score DESC, id DESC LIMIT ?", (limit,),
    ).fetchall()

    order = [g for g in config.genres.priority if g in GENRE_LABELS]
    order += [g for g in GENRE_LABELS if g not in order]
    for genre in order:
        rows = [r for r in report.candidates if (r["genre"] or "unknown") == genre]
        if rows:
            report.sections.append(Section(genre, GENRE_LABELS[genre], rows))

    report.pick = report.candidates[0] if report.candidates else None

    # 今週出したもの。リーチは post reach が毎日書き足す。
    report.published = conn.execute(
        "SELECT o.id, o.kind, o.published_at, o.permalink, o.reach, "
        "p.display_name, p.title, p.location_city, p.location_country, p.genre "
        "FROM posts AS o LEFT JOIN properties AS p ON p.id = o.property_id "
        "WHERE o.state = 'published' AND o.published_at >= ? AND o.published_at < ? "
        "ORDER BY o.published_at",
        (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
    ).fetchall()

    window = (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat())
    report.collected = conn.execute(
        "SELECT COUNT(*) AS n FROM properties WHERE collected_at >= ? AND collected_at < ?",
        window,
    ).fetchone()["n"]
    report.approved = conn.execute(
        "SELECT COUNT(*) AS n FROM properties WHERE reviewed_at >= ? AND reviewed_at < ? "
        "AND status IN ('approved', 'delivered')", window,
    ).fetchone()["n"]
    report.rejected = conn.execute(
        "SELECT COUNT(*) AS n FROM properties WHERE reviewed_at >= ? AND reviewed_at < ? "
        "AND status = 'rejected'", window,
    ).fetchone()["n"]

    for row in report.candidates:
        report.by_country[(row["location_country"] or "不明").strip()] += 1
        report.by_genre[row["genre"] or "unknown"] += 1

    report.checks = _checks(report)
    return report


def _checks(report: WeeklyReport) -> list[Check]:
    """編集方針の最終チェック欄。**足りないことを隠さない。**

    「10件を埋めるために弱い案件を入れない」が方針なので、件数が
    足りないこと自体は失敗ではない。ただし黙って8件にはしない。
    """
    total = len(report.candidates)
    us = report.by_country.get("USA", 0) + report.by_country.get("United States", 0)
    checks = [
        Check(
            f"候補が {TARGET_COUNT} 件あるか", total >= TARGET_COUNT,
            "" if total >= TARGET_COUNT else
            f"{total} 件。**埋めるために弱い案件を入れない**——足りない週は足りないまま出す",
        ),
        Check(
            "米国だけに偏っていないか", total == 0 or us * 2 <= total,
            "" if total == 0 or us * 2 <= total else f"{total} 件中 {us} 件が米国",
        ),
        Check(
            "Hidden Gem が入っているか", report.by_genre.get("hidden_gem", 0) > 0,
        ),
        Check(
            "Conversion（用途変更）が入っているか",
            report.by_genre.get("adaptive_reuse", 0) > 0,
        ),
        Check("Pick of the Week を選んだか", report.pick is not None),
    ]
    return checks


def render(report: WeeklyReport) -> str:
    """端末用。審査UIの画面と同じ中身をテキストで出す。"""
    lines = [
        "FREMING CURATED — WEEKLY REPORT",
        f"  {report.label}（{report.generated_at:%m/%d %H:%M} 時点）",
        "",
        f"今週の入荷 {report.collected} 件 / 承認 {report.approved} / "
        f"非承認 {report.rejected} / 公開 {len(report.published)}",
        "",
        f"■ 今週の候補（未審査の上位 {len(report.candidates)} 件）",
    ]
    for section in report.sections:
        lines.append(f"\n  {section.label}")
        for row in section.rows:
            lines.append(f"    {_one_line(row)}")

    if report.pick is not None:
        lines += ["", "■ Pick of the Week", f"  {_one_line(report.pick)}"]
        if report.pick["summary"]:
            lines.append(f"    {report.pick['summary']}")

    if report.published:
        lines += ["", "■ 今週出したもの"]
        for row in report.published:
            reach = f"リーチ {row['reach']}" if row["reach"] is not None else "リーチ未取得"
            name = row["display_name"] or row["title"] or "（物件不明）"
            lines.append(f"    {(row['published_at'] or '')[:10]}  {name[:40]:<40} {reach}")

    lines += ["", "■ 最終チェック"]
    for check in report.checks:
        mark = "○" if check.ok else "要確認"
        lines.append(f"  [{mark}] {check.label}" + (f" — {check.note}" if check.note else ""))
    return "\n".join(lines)


def _one_line(row: Row) -> str:
    name = row["display_name"] or row["title"] or ""
    place = " / ".join(x for x in (row["location_city"], row["location_country"]) if x)
    bits = [f"#{row['id']:<5}", f"{float(row['score'] or 0):>3.0f}点", name[:44]]
    if place:
        bits.append(f"（{place}）")
    marks = []
    if _flag(row, "style_identified"):
        marks.append("様式")
    if _flag(row, "one_of_a_kind"):
        marks.append("一点物")
    if _flag(row, "provenance_visible"):
        marks.append("前歴")
    if marks:
        bits.append("[" + "/".join(marks) + "]")
    return "  ".join(bits)


__all__ = [
    "GENRE_LABELS",
    "TARGET_COUNT",
    "Check",
    "Section",
    "WeeklyReport",
    "axes_of",
    "build",
    "render",
    "week_bounds",
]
