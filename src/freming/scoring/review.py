"""[7] 承認の実績からスコア付けを検証する。

`docs/approval-criteria.md` の基準は、仕組みが動く前の**8件の実例**から
言語化したもの。以来、審査UIで承認・非承認の判断が積み上がっている。
実績と重み付けが合っているかを、推測ではなく数字で確かめる。

## 何を見るか

軸ごとに「承認された物件の平均点」と「非承認の平均点」を並べる。
**差が大きい軸ほど、人の判断を説明できている。** 差が無い軸は、重みを
掛けても順位を動かしていないだけで、費用も手間も掛かっていない——が、
その重みのぶん、効いている軸の影響を薄めている。

合算後のスコアだけを見ても分からない。0003 のマイグレーションで軸ごとの
内訳（score_detail）を残してあるのはこのため。

## この数字で言えないこと

**採点まで届かなかったものは入っていない。** 収集が拾わなかった物件と、
まだ採点していない候補は、この集計に存在しない。

一方で、**点数が低いものは審査に上がっている。** min_to_persist(30) は
採点ログの件数を数えているだけで、保存も一覧も止めていない
（list_properties の min_score は誰も渡していない）。0点になった候補も
審査UIの pending に並ぶ——既定の並び（score 降順）で末尾になるだけ。

つまり足切りに掛かった候補も人の目には触れており、それを承認したという
事実は、足切りの条件を見直す根拠として使える。**点数の高いものほど
先に読まれる**という偏りは残るので、下位の承認率は実力より低めに出る。
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from freming.values import parse_year

# 承認とみなす状態。納品済みは「承認したうえで次へ進んだ」もの。
APPROVED = ("approved", "delivered")
REJECTED = ("rejected",)


@dataclass
class AxisStat:
    """1つの軸の効き方。"""

    key: str
    weight: float
    approved: list[float] = field(default_factory=list)
    rejected: list[float] = field(default_factory=list)

    @property
    def approved_mean(self) -> float:
        return statistics.fmean(self.approved) if self.approved else 0.0

    @property
    def rejected_mean(self) -> float:
        return statistics.fmean(self.rejected) if self.rejected else 0.0

    @property
    def gap(self) -> float:
        """承認 − 非承認。**正で大きいほど、その軸は判断を説明している。**"""
        return self.approved_mean - self.rejected_mean


@dataclass
class FlagStat:
    """真偽の判定（前歴が見えるか等）が、承認率とどう結び付いているか。"""

    key: str
    approved_true: int = 0
    approved_total: int = 0
    rejected_true: int = 0
    rejected_total: int = 0

    def rate(self, approved: bool) -> float | None:
        total = self.approved_total if approved else self.rejected_total
        if not total:
            return None
        hit = self.approved_true if approved else self.rejected_true
        return 100.0 * hit / total


@dataclass
class ApprovedExample:
    """承認された1件の要点。**プロンプトの実例を書き直すための材料。**

    config.scoring.approved_examples は仕組みが動く前の8件のまま。
    実績で置き換えるには、集計ではなく現物が要る。
    """

    score: float
    name: str
    genre: str
    year: str
    city: str
    architect: str
    style: str
    summary: str

    def line(self) -> str:
        bits = [b for b in (self.style, self.year, self.city) if b]
        head = f"[{self.score:.0f}] {self.name}"
        if self.architect:
            head += f" / {self.architect}"
        if bits:
            head += f"（{' · '.join(bits)}）"
        return f"{head}\n    {self.genre}: {self.summary}" if self.summary else head


@dataclass
class ApprovalReport:
    approved_scores: list[float] = field(default_factory=list)
    rejected_scores: list[float] = field(default_factory=list)
    pending: int = 0
    axes: dict[str, AxisStat] = field(default_factory=dict)
    flags: dict[str, FlagStat] = field(default_factory=dict)
    genres: Counter = field(default_factory=Counter)
    sources: Counter = field(default_factory=Counter)
    ranks: Counter = field(default_factory=Counter)
    decades: Counter = field(default_factory=Counter)
    countries: Counter = field(default_factory=Counter)
    examples: list[ApprovedExample] = field(default_factory=list)
    # 判定が**列に**入っている件数。JSON の中の値とは別に数える。
    # 0019 の埋め戻しが効いたかどうかは、これを見ないと分からない。
    columns_filled: Counter = field(default_factory=Counter)
    approved_gated: int = 0
    # 足切りに掛かったのに承認されたものの、足切りの理由（story / 築年）。
    # **どちらの足切りを見直す話なのかは、ここを見ないと決められない。**
    approved_gate_kinds: Counter = field(default_factory=Counter)
    # 同じく、story の足切りで落ちたのに承認されたものの story 素点。
    # 下限をいくつにすれば拾えたのかが分かる。
    approved_gated_story: list[float] = field(default_factory=list)
    no_detail: int = 0
    # 点数順に並べたとき、上位N件（N=承認数）に承認済みが何件入るか。
    precision_at_k: float | None = None
    missed_high: int = 0    # 承認の中央値より高得点なのに非承認だった件数


def _detail(raw: Any) -> dict | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        found = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def analyze(rows: Sequence[Mapping[str, Any]], weights: Mapping[str, float]) -> ApprovalReport:
    """審査済みの行から実績をまとめる。**DBには触らない。**

    weights は config の重み（軸キー → 重み）。表に併記するだけで、
    集計そのものには使わない。
    """
    report = ApprovalReport()
    scored: list[tuple[float, bool]] = []   # (点数, 承認されたか)

    for row in rows:
        status = (row["status"] or "").strip()
        approved = status in APPROVED
        if status not in APPROVED and status not in REJECTED:
            report.pending += 1
            continue

        score = float(row["score"] or 0.0)
        (report.approved_scores if approved else report.rejected_scores).append(score)
        scored.append((score, approved))

        if approved:
            report.genres[row["genre"] or "unknown"] += 1
            report.sources[row["source"] or "不明"] += 1
            report.ranks[row["source_rank"] or "不明"] += 1
            report.countries[(row["location_country"] or "不明").strip() or "不明"] += 1
            year = parse_year(row["year_built"])
            report.decades[f"{year // 10 * 10}年代" if year else "築年不明"] += 1
            report.examples.append(_example(row, score))

        for column in ("provenance_visible", "style_identified", "one_of_a_kind"):
            try:
                filled = row[column] is not None
            except (KeyError, IndexError):
                continue
            if filled:
                report.columns_filled[column] += 1

        detail = _detail(row["score_detail"])
        if detail is None:
            report.no_detail += 1
            continue

        if approved and detail.get("gate"):
            # 足切りに掛かった（＝0点になった）ものを人が承認していたら、
            # 足切りの条件が厳しすぎる可能性がある。
            report.approved_gated += 1
            _gate_kind(report, str(detail["gate"]))

        for axis in detail.get("axes") or []:
            key = str(axis.get("key") or "")
            if not key:
                continue
            stat = report.axes.setdefault(key, AxisStat(key, float(weights.get(key, 0.0))))
            (stat.approved if approved else stat.rejected).append(float(axis.get("raw") or 0.0))

        for key, value in (detail.get("flags") or {}).items():
            flag = report.flags.setdefault(str(key), FlagStat(str(key)))
            if approved:
                flag.approved_total += 1
                flag.approved_true += 1 if value else 0
            else:
                flag.rejected_total += 1
                flag.rejected_true += 1 if value else 0

    _rank_quality(report, scored)
    return report


def _text(row: Mapping[str, Any], key: str) -> str:
    """行に無い列でも落ちないようにする。

    集計だけを使う呼び出し側（テストや古い問い合わせ）が、実例用の列まで
    揃えなくて済むようにしておく。
    """
    try:
        value = row[key]
    except (KeyError, IndexError):
        return ""
    return (value or "").strip() if isinstance(value, str) else ""


def _example(row: Mapping[str, Any], score: float) -> ApprovedExample:
    return ApprovedExample(
        score=score,
        name=_text(row, "display_name") or _text(row, "title") or f"#{row['id']}",
        genre=_text(row, "genre") or "unknown",
        year=_text(row, "year_built"),
        city=_text(row, "location_city"),
        architect=_text(row, "architect"),
        style=_text(row, "style_name"),
        summary=_text(row, "summary"),
    )


def _gate_kind(report: ApprovalReport, gate: str) -> None:
    """足切りの理由文字列を種類に振り分ける（weights._gate が書く形）。

    `story=20 < 40` / `築年 2021 ≧ 2000` の2種類しか出ない。将来
    条件が増えたときに黙って消えないよう、どちらでもないものは
    理由をそのまま数える。
    """
    if gate.startswith("story="):
        report.approved_gate_kinds["story_min"] += 1
        head = gate[len("story="):].split()[0]
        try:
            report.approved_gated_story.append(float(head))
        except ValueError:
            pass
    elif gate.startswith("築年"):
        report.approved_gate_kinds["built_before"] += 1
    else:
        report.approved_gate_kinds[gate] += 1


def _rank_quality(report: ApprovalReport, scored: list[tuple[float, bool]]) -> None:
    """点数の並びが、人の判断をどれだけ再現するか。

    承認がN件なら、点数上位N件のうち何件が実際に承認されたかを見る。
    100%なら点数だけで選んでよく、低ければ点数は目安にしかなっていない。
    """
    approved_count = len(report.approved_scores)
    if not approved_count or len(scored) < 2:
        return
    ordered = sorted(scored, key=lambda pair: pair[0], reverse=True)
    hit = sum(1 for _, ok in ordered[:approved_count] if ok)
    report.precision_at_k = 100.0 * hit / approved_count

    median = statistics.median(report.approved_scores)
    report.missed_high = sum(1 for s in report.rejected_scores if s > median)


def _spread(values: list[float]) -> str:
    if not values:
        return "—"
    return (f"{min(values):>5.0f}{statistics.median(values):>6.0f}"
            f"{max(values):>6.0f}")


def render(report: ApprovalReport, weights: Mapping[str, float],
           examples: int = 0) -> str:
    """人が読んで判断できる形にする。**数字と、その読み方まで書く。**"""
    out: list[str] = []
    approved = len(report.approved_scores)
    rejected = len(report.rejected_scores)
    out.append(f"審査済み {approved + rejected} 件"
               f"（承認+納品 {approved} / 非承認 {rejected}）"
               f" ／ 未審査 {report.pending} 件")
    if not approved:
        out.append("\n承認された物件がまだありません。実績からは何も言えません。")
        return "\n".join(out)
    if report.no_detail:
        out.append(f"※ うち {report.no_detail} 件は内訳（score_detail）が無く、"
                   f"軸ごとの集計に入っていません（採点前の古い行）。")

    out.append("\n--- 合算スコアの分布 ---")
    out.append(f"{'':<10}{'件数':>5}{'最低':>6}{'中央':>6}{'最高':>6}")
    out.append(f"{'承認済み':<10}{approved:>5}{_spread(report.approved_scores)}")
    out.append(f"{'非承認':<10}{rejected:>5}{_spread(report.rejected_scores)}")
    if report.precision_at_k is not None:
        out.append(f"\n点数上位 {approved} 件のうち、実際に承認されたのは "
                   f"{report.precision_at_k:.0f}%")
        out.append(f"承認の中央値より高得点なのに非承認だった: {report.missed_high} 件")
        if report.precision_at_k < 60:
            out.append("→ **点数の並びは人の判断をあまり説明できていない。** 重みを見直す根拠になる。")
        elif report.precision_at_k >= 80:
            out.append("→ 点数の並びは人の判断とおおむね一致している。")
    if report.approved_gated:
        out.append(f"\n**足切りに掛かったのに承認された物件が {report.approved_gated} 件ある。**"
                   "\n  足切り（story_min / built_before）が厳しすぎる可能性がある。")
        if report.approved_gate_kinds:
            breakdown = " / ".join(f"{k} {v}件"
                                   for k, v in report.approved_gate_kinds.most_common())
            out.append(f"  内訳: {breakdown}")
        if report.approved_gated_story:
            values = sorted(report.approved_gated_story)
            out.append(f"  うち story の足切りで落ちたものの素点: "
                       f"{'/'.join(f'{v:.0f}' for v in values)}")

    out.append("\n--- 軸ごとの効き方（0〜100の素点） ---")
    out.append(f"{'軸':<10}{'承認':>7}{'非承認':>8}{'差':>7}{'重み':>7}   読み方")
    for key, stat in sorted(report.axes.items(), key=lambda kv: -kv[1].gap):
        note = _axis_note(stat)
        out.append(f"{key:<10}{stat.approved_mean:>7.0f}{stat.rejected_mean:>8.0f}"
                   f"{stat.gap:>+7.0f}{stat.weight:>7.2f}   {note}")
    out.append("\n差＝承認された物件の平均 − 非承認の平均。"
               "**正で大きいほど、その軸は人の判断を説明している。**")

    if report.flags:
        out.append("\n--- 判定フラグの持ち方（真だった割合） ---")
        out.append(f"{'':<22}{'承認':>7}{'非承認':>8}{'差':>7}")
        for key, flag in report.flags.items():
            yes = flag.rate(True)
            no = flag.rate(False)
            if yes is None or no is None:
                continue
            out.append(f"{key:<22}{yes:>6.0f}%{no:>7.0f}%{yes - no:>+6.0f}pt")

        # **上の割合は score_detail の JSON から数えている。** 列にも
        # 入っていないと、審査UIでの絞り込みには使えない。
        reviewed = approved + rejected
        missing = [k for k in ("provenance_visible", "style_identified", "one_of_a_kind")
                   if report.columns_filled[k] < reviewed]
        if missing:
            out.append("\n※ 列に入っていない判定がある（絞り込みに使えない）:")
            for key in missing:
                out.append(f"  {key:<22}{report.columns_filled[key]:>4} / {reviewed} 件")

    out.append("\n--- 承認された物件の内訳 ---")
    for label, counter in (
        ("ジャンル", report.genres), ("ソース", report.sources),
        ("ソースのランク", report.ranks), ("築年", report.decades),
        ("国", report.countries),
    ):
        if not counter:
            continue
        line = " / ".join(f"{k} {v}" for k, v in counter.most_common())
        out.append(f"{label:<8}{line}")

    if examples and report.examples:
        # **点数順ではなく点数の高い順に上から。** プロンプトの実例を
        # 書き直すための現物で、集計では代わりにならない。
        top = sorted(report.examples, key=lambda e: -e.score)[:examples]
        out.append(f"\n--- 承認された物件（点数の高い順に {len(top)} 件） ---")
        out.extend(e.line() for e in top)

    out.append("\n" + _suggest(report, weights))
    return "\n".join(out)


def _axis_note(stat: AxisStat) -> str:
    if not stat.approved or not stat.rejected:
        return "比較材料が足りない"
    if stat.gap >= 15:
        return "効いている"
    if stat.gap <= -15:
        return "**逆に効いている**（高いほど非承認）"
    if abs(stat.gap) < 5:
        return "差がない（重みが順位を動かしていない）"
    return "弱い"


def _suggest(report: ApprovalReport, weights: Mapping[str, float]) -> str:
    """重みの案。**そのまま適用しない。** 根拠と一緒に人が判断する。

    差（承認 − 非承認）に比例させた配分と、いまの重みの中間を取る。
    実績だけに寄せ切らないのは、件数がまだ少ないため。1件の増減で
    配分が動くようでは設定として使えない。
    """
    usable = {k: s for k, s in report.axes.items() if s.approved and s.rejected}
    if not usable:
        return "重みの案: 比較できる軸がありません。"

    # **案は、比較できる軸が持っている重みの中だけで配り直す。**
    # 足したばかりの軸は既存の score_detail に入っていないので実績が無い。
    # 全体を 1.0 に正規化すると、その軸の重みを毎回まるごと取り上げる案に
    # なる（style/one_of_a_kind を足した直後に実際そうなった）。
    budget = sum(float(weights.get(k, 0.0)) for k in usable)
    reserved = sorted(k for k in weights if k not in usable and weights[k] > 0)
    if budget <= 0:
        return "重みの案: 比較できる軸に重みが付いていません。"

    positive = {k: max(s.gap, 0.0) for k, s in usable.items()}
    total = sum(positive.values())
    if total <= 0:
        return ("重みの案: **どの軸も承認と非承認を分けていません。**\n"
                "  重みの調整では直りません。軸そのもの（何を見るか）を変える話になります。")

    lines = [f"重みの案（実績に比例させた配分と、いまの重みの中間 / 合計 {budget:.2f} の中で）", ""]
    lines.append(f"{'軸':<10}{'いま':>7}{'案':>7}   根拠")
    fresh: dict[str, float] = {}
    for key, stat in sorted(usable.items(), key=lambda kv: -kv[1].gap):
        now = float(weights.get(key, 0.0))
        by_gap = positive[key] / total * budget
        blended = round((now + by_gap) / 2 * 20) / 20   # 0.05 刻み
        fresh[key] = max(blended, 0.05)
    # 比較できる軸が持っていた合計に戻す。丸めの誤差は最も効いている軸で吸収。
    scale = sum(fresh.values())
    top = max(fresh, key=lambda k: usable[k].gap)
    for key in fresh:
        fresh[key] = round(fresh[key] / scale * budget * 20) / 20
    fresh[top] = round(fresh[top] + (budget - sum(fresh.values())), 2)

    for key, stat in sorted(usable.items(), key=lambda kv: -kv[1].gap):
        now = float(weights.get(key, 0.0))
        mark = "→" if abs(fresh[key] - now) >= 0.05 else " "
        lines.append(f"{key:<10}{now:>7.2f}{fresh[key]:>7.2f} {mark} 差 {stat.gap:+.0f}")
    lines.append("")
    if reserved:
        held = ", ".join(f"{k} {weights[k]:.2f}" for k in reserved)
        lines.append(f"残り {1.0 - budget:.2f}（{held}）はこの案の対象外です。")
        lines.append("**足したばかりの軸は、既存の score_detail に入っていないので")
        lines.append("比較材料がありません。** 採点が一巡すれば表に載ります。")
        lines.append("")
    lines.append("**この案は採点まで届いた候補の中での選ばれ方しか反映していません。**")
    lines.append("収集が拾わなかった物件は入っていません。また既定の並びが点数順の")
    lines.append("ため、点数の低い候補は読まれる順が後ろになり、承認率が実力より")
    lines.append("低めに出ます。")
    return "\n".join(lines)


__all__ = ["ApprovalReport", "AxisStat", "FlagStat", "analyze", "render"]
