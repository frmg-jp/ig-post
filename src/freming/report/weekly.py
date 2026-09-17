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
  - **考察**: 数字の差だけを並べる。**因果は言わない。**本数が足りない
    うちは「まだ言えない」と書く（MIN_TOTAL / MIN_GROUP）
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

# **考察は毎週かならず何か出す。** 以前は「全体8本・群ごと3本・差15%」を
# 割ると何も言わずに「はっきりした差はありません」だけを出していた。
# 本数がこの規模では毎週それになり、欄として死んでいた（2026-09-17 の指摘）。
#
# いまは**差の大きい順に並べて、確度の札を貼って出す。** 断定はしないが、
# 黙りもしない。札の意味:
#
#   傾向 … 群ごと MIN_GROUP 本以上、差が LIFT_TREND 以上。次の選定に使える
#   仮説 … 差が LIFT_HINT 以上。1本の当たり外れで消える程度の差
#   参考 … それ未満、または群が小さい。**今のところ差は無い**という情報
MIN_PAIR = 2      # 比べるのに最低これだけ要る（これ未満は出さない）
MIN_GROUP = 3     # 「傾向」を名乗れる本数
MIN_TOTAL = 6     # 全体について何か言うのに要る本数
LIFT_TREND = 1.15  # これ以上なら「傾向」
LIFT_HINT = 1.05   # これ以上なら「仮説」
TOP_INSIGHTS = 4   # 出す数。多すぎると全部が薄くなる

# 並べる順。**確度が先、差の大きさは後。** 差だけで並べると、片側2本の
# 大きな差が、両側10本の確かな差より上に来る。
_STRENGTH_ORDER = {"傾向": 0, "仮説": 1, "参考": 2}

# 互換のため残す（外から参照されている）。
MIN_LIFT = LIFT_TREND

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
class Insight:
    """数字から言えること。**根拠の数字を必ず持つ。**

    ここに入るのは「AとBで平均がこれだけ違った」という観測だけで、
    因果ではない。写真の枚数が多い投稿が伸びていたとしても、枚数が
    理由とは限らない（枚数を出せる物件は、そもそも写真が良い）。
    提案は「次に試す価値がある」までにとどめる。

    strength は確度の札（傾向／仮説／参考）。**小さい差も出すが、
    小さいことを隠さない。**
    """

    headline: str
    evidence: str
    suggestion: str = ""
    strength: str = ""
    lift: float = 1.0


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
    insights: list[Insight] = field(default_factory=list)
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

    report.insights = _insights(conn, end)
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


# 考察の材料。**通常投稿だけ**を見る。リールは作り方も出し方も違うので、
# 同じ土俵に乗せると平均が意味を失う。
_TREND_ROWS = """
SELECT o.reach AS reach, o.published_at AS published_at,
       p.genre AS genre, p.style_identified AS style_identified,
       p.one_of_a_kind AS one_of_a_kind,
       p.provenance_visible AS provenance_visible,
       p.architect AS architect, p.year_built_value AS year_built_value,
       p.location_country AS location_country, p.score AS score,
       (SELECT COUNT(*) FROM images i WHERE i.property_id = p.id) AS image_count
  FROM posts AS o
  JOIN properties AS p ON p.id = o.property_id
 WHERE o.state = 'published' AND o.kind = 'feed' AND o.reach IS NOT NULL
   AND o.published_at >= ? AND o.published_at < ?
"""


def _avg(values: list[int]) -> float:
    return sum(values) / len(values) if values else 0.0


@dataclass
class Split:
    """2つに割って平均を比べた結果。**高い方を left に寄せてある。**"""

    left: str          # 高かった側の名前
    right: str         # 低かった側の名前
    left_avg: float
    right_avg: float
    n_left: int
    n_right: int

    @property
    def lift(self) -> float:
        return self.left_avg / self.right_avg if self.right_avg else 1.0

    @property
    def gap_pct(self) -> int:
        return round((self.lift - 1) * 100)

    @property
    def n_min(self) -> int:
        return min(self.n_left, self.n_right)

    @property
    def strength(self) -> str:
        """**確度の札。** 小さい差も出すが、小さいことを隠さない。"""
        if self.n_min >= MIN_GROUP and self.lift >= LIFT_TREND:
            return "傾向"
        if self.lift >= LIFT_HINT:
            return "仮説"
        return "参考"

    @property
    def evidence(self) -> str:
        line = (f"{self.left} {self.left_avg:.0f}（{self.n_left}本）／ "
                f"{self.right} {self.right_avg:.0f}（{self.n_right}本）"
                f"／ 差 {self.gap_pct}%")
        if self.strength == "傾向":
            return line
        if self.strength == "参考":
            return line + "。ほぼ差はありません"
        # **どこが弱いのかを書く。** 差が小さいのか、片側が薄いのか。
        if self.n_min < MIN_GROUP:
            return line + f"。片側が{self.n_min}本しかないので、1本の入れ替わりで消えます"
        return line + "。1本の当たり外れで消える程度の差です"


def _split(rows: list[Row], pick, left: str, right: str) -> Split | None:
    """条件で2つに割る。**どちらかが MIN_PAIR 本未満なら比べない。**

    以前は MIN_GROUP（3本）未満を切っていたが、それだと出る週がほとんど
    無かった。2本ずつでも並べて、確度の札で弱さを示すほうが読める。
    """
    yes = [int(r["reach"]) for r in rows if pick(r)]
    no = [int(r["reach"]) for r in rows if not pick(r)]
    if len(yes) < MIN_PAIR or len(no) < MIN_PAIR:
        return None
    a, b = _avg(yes), _avg(no)
    if a >= b:
        return Split(left, right, a, b, len(yes), len(no))
    return Split(right, left, b, a, len(no), len(yes))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if not ordered:
        return 0.0
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _stock(conn: DbConnection, where: str, params: tuple = ()) -> int:
    """未審査でいますぐ回せる在庫。**提案に「何件あるか」を添えるため。**"""
    try:
        return int(conn.execute(
            "SELECT COUNT(*) AS n FROM properties "
            f"WHERE status = 'pending' AND score IS NOT NULL AND {where}",
            params,
        ).fetchone()["n"])
    except Exception:  # noqa: BLE001 - 列が無い環境でも考察は出す
        return 0


def _axes(conn: DbConnection, rows: list[Row]) -> list[Insight]:
    """**割れる軸を全部試して、差の大きい順に並べる。**

    以前は軸ごとに「15%を超えたら出す」としていたので、この規模だと
    どれも超えず、毎週「はっきりした差はありません」だけが出ていた。
    いまは超えなくても出し、確度の札（傾向／仮説／参考）で弱さを示す。
    """
    out: list[Insight] = []

    def add(split: Split | None, headline, suggestion) -> None:
        if split is None:
            return
        out.append(Insight(
            headline=headline(split),
            evidence=split.evidence,
            suggestion=suggestion(split),
            strength=split.strength,
            lift=split.lift,
        ))

    # 1. ジャンル。いちばん平均が高い群 vs それ以外。在庫の件数を添える。
    buckets: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        buckets[row["genre"] or "unknown"].append(int(row["reach"]))
    usable = {g: v for g, v in buckets.items() if len(v) >= MIN_PAIR}
    if usable:
        top = max(usable, key=lambda g: _avg(usable[g]))
        label = GENRE_LABELS.get(top, top)
        stock = _stock(conn, "genre = ?", (top,))
        add(
            _split(rows, lambda r: (r["genre"] or "unknown") == top, label, "それ以外"),
            lambda s: f"{s.left} のほうが平均が高い",
            lambda s: (
                f"未審査に {label} が {stock} 件あります。来週の枠を"
                f"ここから多めに取ると、同じ向きが続くか確かめられます"
                if s.left == label and stock else
                f"未審査に {label} の在庫がありません。収集ソースを増やす話になります"
                if s.left == label else
                f"{label} の枠を減らして、他のジャンルに回す価値があります"
            ),
        )

    # 2. 写真の枚数。**枚数が理由とは限らない**——枚数を出せる物件は、
    #    そもそも写真が良いことが多い。
    thin = _stock(conn, "id IN (SELECT property_id FROM images GROUP BY "
                        "property_id HAVING COUNT(*) <= 7)")
    add(
        _split(rows, lambda r: (r["image_count"] or 0) >= 8, "8枚以上", "7枚以下"),
        lambda s: f"写真が{s.left}のほうが平均が高い",
        lambda s: (
            f"枚数が理由とは限りません（枚数を出せる物件は写真も良い）。"
            f"7枚以下の在庫が {thin} 件あります。画像補完で枚数を揃えてから出すと、"
            f"どちらなのか切り分けられます" if s.left == "8枚以上" and thin else
            "枚数が理由とは限りません。枚数の少ない物件を避けずに出して確かめられます"
        ),
    )

    # 3. 承認の実績で効いていた判定が、リーチでも効いているか。
    #    **札の対を明示する。** 「前歴が見える」＋「なし」を繋ぐと
    #    「前歴が見えるなし」になっていた（2026-09-17）。
    for key, yes_label, no_label in (
        ("style_identified", "様式が特定できるもの", "様式が特定できないもの"),
        ("one_of_a_kind", "一点物", "一点物でないもの"),
        ("provenance_visible", "前歴が見えるもの", "前歴が見えないもの"),
    ):
        add(
            _split(rows, lambda r, k=key: bool(r[k]), yes_label, no_label),
            lambda s, y=yes_label: f"{s.left}のほうが平均が高い" + (
                "" if s.left == y else "（審査の基準とは逆向き）"
            ),
            lambda s, y=yes_label: (
                f"審査で「{y}」を重く見ているのは、リーチの側からも支持されています"
                if s.left == y else
                "審査の基準は承認の実績から決めたものです。すぐには変えません。"
                "本数が増えても同じ向きが続くなら、そのとき見直します"
            ),
        )

    # 4. 国。米国に偏りやすいので、偏りが得なのか損なのかを見る。
    add(
        _split(rows, lambda r: (r["location_country"] or "") == "United States",
               "米国", "米国以外"),
        lambda s: f"{s.left}のほうが平均が高い",
        lambda s: (
            "出しているものの多くが米国です。米国以外を増やすと本数の偏りは"
            "直りますが、リーチは下がるかもしれません" if s.left == "米国" else
            "米国以外を増やす理由になります。いまは米国が多いので、"
            "枠を分けて試す価値があります"
        ),
    )

    # 5. 築年。古いほうが効くのか、新しいほうが効くのか。
    years = [int(r["year_built_value"]) for r in rows if r["year_built_value"]]
    if len(years) >= MIN_PAIR * 2:
        line = int(_median([float(y) for y in years]))
        add(
            _split(rows,
                   lambda r: bool(r["year_built_value"])
                   and int(r["year_built_value"]) < line,
                   f"{line}年より前", f"{line}年以降"),
            lambda s: f"築年が{s.left}のほうが平均が高い",
            lambda s: f"来週の枠を{s.left}に寄せると、続くかどうかが分かります",
        )

    # 6. 設計者名。名前が立つ物件のほうが強いのか。
    add(
        _split(rows, lambda r: bool((r["architect"] or "").strip()),
               "設計者が分かる", "設計者が不明な"),
        lambda s: f"{s.left}もののほうが平均が高い",
        lambda s: (
            "設計者名が本文の見出しに立ちます。名前のある物件を優先する価値があります"
            if s.left == "設計者が分かる" else
            "設計者名は、いまのところリーチとは結びついていません"
        ),
    )

    # 7. 採点。**審査の点数が、実際の反応と合っているか。**
    scores = [float(r["score"]) for r in rows if r["score"] is not None]
    if len(scores) >= MIN_PAIR * 2:
        line = _median(scores)
        add(
            _split(rows,
                   lambda r: r["score"] is not None and float(r["score"]) >= line,
                   f"{line:.0f}点以上", f"{line:.0f}点未満"),
            lambda s: (
                f"点数が{s.left}のほうが平均が高い" + (
                    "" if s.left.endswith("以上") else "（採点とは逆向き）"
                )
            ),
            lambda s: (
                "採点が反応を当てられています。いまの基準を続けて問題ありません"
                if s.left.endswith("以上") else
                "点数の高いものが伸びていません。採点が見ている軸と、"
                "実際に見られる理由がずれている可能性があります"
            ),
        )

    return out


def _insights(conn: DbConnection, end: datetime) -> list[Insight]:
    """**毎週かならず何か出す。** 断定はしないが、黙りもしない。

    差の大きい順に {TOP_INSIGHTS} 本まで並べ、それぞれに確度の札を貼る。
    小さい差も出すが、**小さいことは隠さない**（「仮説」「参考」と書く）。
    """
    since = (end - timedelta(weeks=TREND_WEEKS)).astimezone(UTC).isoformat()
    rows = conn.execute(
        _TREND_ROWS, (since, end.astimezone(UTC).isoformat())
    ).fetchall()

    if len(rows) < MIN_TOTAL:
        return [Insight(
            "まだ何も言えません",
            f"直近{TREND_WEEKS}週でリーチが取れている通常投稿は {len(rows)} 本。"
            f"比べるには全体で {MIN_TOTAL} 本が要ります",
            "毎日の記録が溜まれば自動で出ます。待つのが正解です",
            strength="参考",
        )]

    overall = _avg([int(r["reach"]) for r in rows])
    found = sorted(
        _axes(conn, rows),
        key=lambda i: (_STRENGTH_ORDER.get(i.strength, 9), -i.lift),
    )
    out = found[:TOP_INSIGHTS]

    if not out:
        # どの軸も割れなかった（全部が同じ属性）。それ自体が読める情報。
        return [Insight(
            "比べられる軸がありません",
            f"直近{TREND_WEEKS}週 {len(rows)}本・平均 {overall:.0f}。"
            "ジャンル・写真の枚数・判定のどれも、片側に寄っています",
            "違う種類のものを混ぜて出すと、何が効くのかが見えるようになります",
            strength="参考",
        )]

    # 全部が「参考」だったときだけ、そう明記する。**黙って終わらない。**
    if all(i.strength == "参考" for i in out):
        out.insert(0, Insight(
            "どれを出しても同じくらい見られています",
            f"直近{TREND_WEEKS}週 {len(rows)}本・平均 {overall:.0f}。"
            f"いちばん大きい差でも {out[0].lift * 100 - 100:.0f}%",
            "いまは何を出すかより、本数と写真の枚数を安定させるほうが効きます",
            strength="参考",
        ))
    return out


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

    if report.insights:
        lines += ["", "■ 考察"]
        for insight in report.insights:
            mark = f"[{insight.strength}] " if insight.strength else ""
            lines.append(f"  {mark}{insight.headline}")
            lines.append(f"    根拠: {insight.evidence}")
            if insight.suggestion:
                lines.append(f"    次に: {insight.suggestion}")

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
    "LIFT_HINT",
    "LIFT_TREND",
    "MIN_GROUP",
    "MIN_PAIR",
    "MIN_TOTAL",
    "TOP_INSIGHTS",
    "TREND_WEEKS",
    "Check",
    "GenreStat",
    "Insight",
    "Split",
    "WeeklyReport",
    "axes_of",
    "build",
    "judgement",
    "marks_of",
    "name_of",
    "render",
    "week_bounds",
]
