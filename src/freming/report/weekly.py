"""[10] 週次レポート——**出したものがどうだったか**。

    今週出した1本ずつ（写真・リーチ・カルテの抜粋） → 数字 → 先週との比較

最初は「未審査の上位10件」を並べていたが、**それは未審査タブの仕事**で、
週次で見たいことではない。この画面は振り返り——出した投稿が実際にどう
だったかを1か所で読むためのものにする（2026-09-16 の指摘）。

中身:

  - **今週出したもの**: 1本ずつ、表紙・物件名・リーチと、**投稿カルテの
    抜粋**（点数と判定の理由・設計者・様式・写真の枚数・販売状況）
  - **今週の数字**: 本数・リーチの合計と平均、いちばん見られた1本
  - **先週との比較**: 合計と平均の増減。**リーチは時間とともに伸びる**ので、
    出したばかりの週は不利になる。そう画面にも書く
  - **ジャンル別の平均**（直近8週）: 何が効いているかの手がかり。件数が
    少ないうちは断定しない
  - **今週の振り返り**: 出したものの偏り

外へは一切出ない（DBを読むだけ）。何度開いても同じ。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from freming.config import Config
from freming.db.connection import DbConnection, Row

# ジャンル別の平均を見る期間。短すぎると1本の当たり外れで動く。
TREND_WEEKS = 8

# 見出しに使う日本語。genre の値は scoring/schema.py の GENRES。
GENRE_LABELS = {
    "architect": "Architect-Designed",
    "adaptive_reuse": "Conversion（用途変更）",
    "hidden_gem": "Hidden Gem",
    "penthouse": "Penthouse",
    "loft": "Loft",
    "unknown": "その他",
}

KIND_LABELS = {"feed": "通常", "story": "ストーリーズ", "reel": "リール"}


@dataclass
class Check:
    """今週の振り返り。1行ずつ ○／要確認 を出す。"""

    label: str
    ok: bool
    note: str = ""


@dataclass
class GenreStat:
    genre: str
    label: str
    posts: int
    reach_avg: float


@dataclass
class WeeklyReport:
    start: datetime          # 週の初日（現地時刻）
    end: datetime            # 週の最終日の翌日0時（現地時刻）
    generated_at: datetime
    published: list[Row] = field(default_factory=list)
    best: Row | None = None          # いちばん見られた1本
    reach_total: int = 0
    reach_avg: float = 0.0
    measured: int = 0                # リーチが読めている本数
    prev_total: int = 0
    prev_avg: float = 0.0
    prev_count: int = 0
    genres: list[GenreStat] = field(default_factory=list)
    by_country: Counter = field(default_factory=Counter)
    by_genre: Counter = field(default_factory=Counter)
    # 在庫は1行だけ。**並べない**（未審査タブで見る）。
    pending: int = 0
    approved_waiting: int = 0
    collected: int = 0
    checks: list[Check] = field(default_factory=list)

    @property
    def label(self) -> str:
        last = self.end - timedelta(days=1)
        return f"{self.start:%Y/%m/%d}（月）〜 {last:%m/%d}（日）"

    @property
    def total_delta(self) -> int:
        return self.reach_total - self.prev_total

    @property
    def avg_delta(self) -> float:
        return self.reach_avg - self.prev_avg


def week_bounds(config: Config, now: datetime) -> tuple[datetime, datetime]:
    """その日を含む週の月曜0時と、翌週の月曜0時（現地時刻）。"""
    zone = ZoneInfo(config.instagram.timezone)
    local = now.astimezone(zone)
    monday = datetime.combine(
        local.date() - timedelta(days=local.weekday()), time.min, tzinfo=zone
    )
    return monday, monday + timedelta(days=7)


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


# 出した1本について読むもの。**カルテの抜粋をここで作れるだけ引く。**
# カルテを開かなくても、その投稿が何だったのか・何を評価して出したのかが
# 分かるようにする（2026-09-16 の指摘）。
_PUBLISHED = """
SELECT o.id, o.kind, o.published_at, o.permalink, o.reach, o.reach_checked_at,
       o.note, o.property_id,
       p.display_name, p.title, p.genre, p.location_city, p.location_country,
       p.listing_status, p.score, p.score_detail, p.summary,
       p.architect, p.year_built, p.style_name, p.usage_type,
       p.style_identified, p.one_of_a_kind, p.provenance_visible,
       (SELECT COUNT(*) FROM images i WHERE i.property_id = p.id) AS image_count,
       (SELECT COUNT(*) FROM images i WHERE i.property_id = p.id
         AND i.origin_url IS NOT NULL) AS foreign_images,
       (SELECT i.source_url FROM images i WHERE i.property_id = p.id
         ORDER BY i.position LIMIT 1) AS cover
  FROM posts AS o
  LEFT JOIN properties AS p ON p.id = o.property_id
 WHERE o.state = 'published' AND o.published_at >= ? AND o.published_at < ?
 ORDER BY o.published_at
"""


def _published(conn: DbConnection, start: datetime, end: datetime) -> list[Row]:
    return conn.execute(
        _PUBLISHED, (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat())
    ).fetchall()


def _reach_values(rows: list[Row]) -> list[int]:
    """**未取得は数に入れない。** 0 として平均に混ぜると実態より下がる。"""
    return [int(r["reach"]) for r in rows if r["reach"] is not None]


def build(config: Config, conn: DbConnection, now: datetime | None = None) -> WeeklyReport:
    """レポートを組み立てる。**DBを読むだけ。**"""
    now = now or datetime.now(UTC)
    start, end = week_bounds(config, now)
    report = WeeklyReport(
        start=start, end=end,
        generated_at=now.astimezone(ZoneInfo(config.instagram.timezone)),
    )

    report.published = _published(conn, start, end)
    values = _reach_values(report.published)
    report.measured = len(values)
    report.reach_total = sum(values)
    report.reach_avg = (report.reach_total / len(values)) if values else 0.0
    measured_rows = [r for r in report.published if r["reach"] is not None]
    report.best = max(measured_rows, key=lambda r: int(r["reach"]), default=None)

    prev = _published(conn, start - timedelta(days=7), start)
    prev_values = _reach_values(prev)
    report.prev_count = len(prev)
    report.prev_total = sum(prev_values)
    report.prev_avg = (report.prev_total / len(prev_values)) if prev_values else 0.0

    report.genres = _genre_stats(conn, end)

    for row in report.published:
        report.by_country[(row["location_country"] or "不明").strip()] += 1
        report.by_genre[row["genre"] or "unknown"] += 1

    window = (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat())
    report.collected = conn.execute(
        "SELECT COUNT(*) AS n FROM properties WHERE collected_at >= ? AND collected_at < ?",
        window,
    ).fetchone()["n"]
    report.pending = conn.execute(
        "SELECT COUNT(*) AS n FROM properties WHERE status = 'pending'"
    ).fetchone()["n"]
    report.approved_waiting = conn.execute(
        "SELECT COUNT(*) AS n FROM properties WHERE status IN ('approved', 'delivered')"
    ).fetchone()["n"]

    report.checks = _checks(report)
    return report


def _genre_stats(conn: DbConnection, end: datetime) -> list[GenreStat]:
    """ジャンル別の平均リーチ（直近 TREND_WEEKS 週）。

    **今週だけでは何も言えない。** 1週間に出るのは数本で、ジャンルごとに
    見れば1本ずつになる。数週ぶんためて、ようやく傾向の手がかりになる。
    """
    since = (end - timedelta(weeks=TREND_WEEKS)).astimezone(UTC).isoformat()
    rows = conn.execute(
        "SELECT p.genre AS genre, o.reach AS reach FROM posts AS o "
        "JOIN properties AS p ON p.id = o.property_id "
        "WHERE o.state = 'published' AND o.reach IS NOT NULL "
        "AND o.published_at >= ? AND o.published_at < ?",
        (since, end.astimezone(UTC).isoformat()),
    ).fetchall()

    buckets: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        buckets[row["genre"] or "unknown"].append(int(row["reach"]))
    stats = [
        GenreStat(
            genre=genre,
            label=GENRE_LABELS.get(genre, genre),
            posts=len(values),
            reach_avg=sum(values) / len(values),
        )
        for genre, values in buckets.items()
    ]
    return sorted(stats, key=lambda s: s.reach_avg, reverse=True)


def _checks(report: WeeklyReport) -> list[Check]:
    """今週の振り返り。**出したものについて見る。**

    以前は未審査の候補について見ていたが、この画面は振り返りなので、
    確かめるのは「出したもの」。
    """
    posts = len(report.published)
    us = report.by_country.get("USA", 0) + report.by_country.get("United States", 0)
    unmeasured = posts - report.measured
    top_genre = report.by_genre.most_common(1)[0] if report.by_genre else None

    return [
        Check(
            "リーチが全部読めているか", unmeasured == 0,
            "" if unmeasured == 0 else
            f"{posts} 本中 {unmeasured} 本が未取得。出したばかりの投稿はまだ集計されていない",
        ),
        Check(
            "米国だけに偏っていないか", posts == 0 or us * 2 <= posts,
            "" if posts == 0 or us * 2 <= posts else f"{posts} 本中 {us} 本が米国",
        ),
        Check(
            "ジャンルが偏っていないか",
            top_genre is None or posts <= 2 or top_genre[1] * 2 <= posts,
            "" if top_genre is None or posts <= 2 or top_genre[1] * 2 <= posts else
            f"{posts} 本中 {top_genre[1]} 本が {GENRE_LABELS.get(top_genre[0], top_genre[0])}",
        ),
    ]


def judgement(row: Row) -> str:
    """**なぜこれを出したのか**を1行で。採点の story 軸の理由を抜く。

    軸の中で story だけが、LLM が記事を読んで書いた文。他は機械的に
    決まる（ソースのランク・ジャンル・地域・価格）ので、振り返りの
    材料にならない。
    """
    axes = {key: (raw, reason) for key, raw, reason in axes_of(row)}
    if "story" in axes and axes["story"][1]:
        return axes["story"][1]
    for _key, (_raw, reason) in axes.items():
        if reason:
            return reason
    return ""


def marks_of(row: Row) -> list[str]:
    """承認の実績でいちばん効いていた判定（approval-report 2026-09-04）。"""
    out = []
    for key, label in (
        ("style_identified", "様式の特定"),
        ("one_of_a_kind", "一点物"),
        ("provenance_visible", "前歴が見える"),
    ):
        try:
            if row[key]:
                out.append(label)
        except (KeyError, IndexError, TypeError):
            pass
    return out


def name_of(row: Row) -> str:
    if row["kind"] == "reel":
        return "週次リール"
    return row["display_name"] or row["title"] or "（物件不明）"


def render(report: WeeklyReport) -> str:
    """端末用。審査UIの画面と同じ中身をテキストで出す。"""
    lines = [
        "FREMING CURATED — WEEKLY REPORT",
        f"  {report.label}（{report.generated_at:%m/%d %H:%M} 時点）",
        "",
        "■ 今週の数字",
        f"  出した {len(report.published)} 本（先週 {report.prev_count} 本）",
        f"  リーチ合計 {report.reach_total}（先週 {report.prev_total} / "
        f"{report.total_delta:+d}）",
        f"  1本あたり {report.reach_avg:.0f}（先週 {report.prev_avg:.0f} / "
        f"{report.avg_delta:+.0f}）",
    ]
    if report.measured < len(report.published):
        lines.append(
            f"  ※ {len(report.published) - report.measured} 本はリーチ未取得。"
            "平均には入れていない"
        )

    if report.best is not None:
        lines += [
            "", "■ いちばん見られた1本",
            f"  {name_of(report.best)}（リーチ {report.best['reach']}）",
        ]

    if report.published:
        lines += ["", "■ 今週出したもの"]
        for row in report.published:
            reach = f"リーチ {row['reach']}" if row["reach"] is not None else "リーチ未取得"
            photos = f"  写真 {row['image_count'] or 0}枚" if row["property_id"] else ""
            lines.append(
                f"  {(row['published_at'] or '')[:10]}  {name_of(row)[:38]:<38} "
                f"{reach}{photos}"
            )
            bits = []
            if row["property_id"]:
                bits.append(f"{float(row['score'] or 0):.0f}点")
            bits += marks_of(row)
            for key in ("architect", "year_built", "style_name"):
                if row[key]:
                    bits.append(str(row[key]))
            if bits:
                lines.append("      " + " / ".join(bits))
            reason = judgement(row)
            if reason:
                lines.append(f"      判定: {reason[:70]}")

    if report.genres:
        lines += ["", f"■ ジャンル別の平均リーチ（直近{TREND_WEEKS}週）"]
        for stat in report.genres:
            lines.append(f"  {stat.label:<22} {stat.reach_avg:>5.0f}（{stat.posts}本）")
        lines.append("  ※ 本数が少ないうちは、1本の当たり外れで順位が動く")

    lines += ["", "■ 今週の振り返り"]
    for check in report.checks:
        mark = "○" if check.ok else "要確認"
        lines.append(f"  [{mark}] {check.label}" + (f" — {check.note}" if check.note else ""))

    lines += [
        "", "■ 在庫",
        f"  未審査 {report.pending} 件（今週の入荷 {report.collected}）/ "
        f"投稿に回せる {report.approved_waiting} 件",
    ]
    return "\n".join(lines)


__all__ = [
    "GENRE_LABELS",
    "KIND_LABELS",
    "TREND_WEEKS",
    "Check",
    "GenreStat",
    "WeeklyReport",
    "axes_of",
    "build",
    "judgement",
    "marks_of",
    "name_of",
    "render",
    "week_bounds",
]
