"""[7] 承認の実績からスコア付けを検証する仕組みの検証。

**数字が合っていないレポートは、無いより悪い。** これを根拠に重みを
動かすので、集計そのものを固定しておく。
"""

from __future__ import annotations

import json

import pytest

from freming.scoring.review import analyze, render

WEIGHTS = {
    "story": 0.25, "source": 0.15, "for_sale": 0.20,
    "genre": 0.15, "area": 0.20, "price": 0.05,
}


def _row(status, score, *, story=50, area=50, price=0, gate="",
         provenance=True, genre="adaptive_reuse", source="dezeen",
         rank="S", year="1900", country="US", detail=True):
    """properties の1行。score_detail は採点が書く形をそのまま作る。"""
    payload = {
        "gate": gate,
        "axes": [
            {"key": "story", "raw": story, "weight": 0.25, "reason": ""},
            {"key": "area", "raw": area, "weight": 0.20, "reason": ""},
            {"key": "price", "raw": price, "weight": 0.05, "reason": ""},
        ],
        "flags": {"provenance_visible": provenance},
        "provenance_note": "",
    }
    return {
        "id": 1, "source": source, "source_rank": rank, "status": status,
        "score": score, "score_detail": json.dumps(payload) if detail else None,
        "genre": genre, "year_built": year, "price": "$1",
        "location_city": "LA", "location_country": country,
    }


def test_承認と非承認を数える():
    report = analyze(
        [_row("approved", 80), _row("delivered", 75), _row("rejected", 40),
         _row("pending", 60)],
        WEIGHTS,
    )
    assert len(report.approved_scores) == 2   # delivered も承認に数える
    assert len(report.rejected_scores) == 1
    assert report.pending == 1


def test_軸ごとの差が出る():
    """**これがレポートの中心。** 承認と非承認で素点がどう違うか。"""
    rows = [
        _row("approved", 80, story=90, area=20),
        _row("approved", 78, story=85, area=30),
        _row("rejected", 50, story=30, area=90),
        _row("rejected", 45, story=35, area=80),
    ]
    report = analyze(rows, WEIGHTS)

    story = report.axes["story"]
    assert story.approved_mean == pytest.approx(87.5)
    assert story.rejected_mean == pytest.approx(32.5)
    assert story.gap == pytest.approx(55.0)

    # エリアは**逆に効いている**（高いほど非承認）。ここを見落とすと、
    # 重みを上げるほど順位が人の判断から離れる。
    assert report.axes["area"].gap == pytest.approx(-60.0)


def test_逆に効いている軸は文面で警告する():
    rows = [_row("approved", 80, area=10), _row("rejected", 50, area=95)]
    text = render(analyze(rows, WEIGHTS), WEIGHTS)
    assert "逆に効いている" in text


def test_点数の並びが人の判断と合っているかを出す():
    """点数上位N件（N=承認数）に承認済みが何件入るか。"""
    rows = [
        _row("approved", 90), _row("approved", 85),
        _row("rejected", 80), _row("rejected", 20),
    ]
    report = analyze(rows, WEIGHTS)
    assert report.precision_at_k == pytest.approx(100.0)

    # 点数の高いものが軒並み非承認なら、並びは説明できていない
    flipped = [
        _row("approved", 30), _row("approved", 20),
        _row("rejected", 95), _row("rejected", 90),
    ]
    assert analyze(flipped, WEIGHTS).precision_at_k == pytest.approx(0.0)


def test_足切りに掛かったのに承認された件数を出す():
    """**足切りが厳しすぎるサイン。** 見落とすと良い候補を捨て続ける。"""
    rows = [_row("approved", 0, gate="story=20 < 40"), _row("rejected", 50)]
    report = analyze(rows, WEIGHTS)
    assert report.approved_gated == 1
    assert "足切りに掛かったのに承認された物件が 1 件" in render(report, WEIGHTS)


def test_内訳の無い古い行は軸の集計に入れない():
    """採点前の行を混ぜると平均が狂う。件数だけ出して除く。"""
    rows = [_row("approved", 80, story=90), _row("approved", 70, detail=False),
            _row("rejected", 40, story=10)]
    report = analyze(rows, WEIGHTS)
    assert report.no_detail == 1
    assert len(report.axes["story"].approved) == 1   # 内訳のある1件だけ
    assert len(report.approved_scores) == 2          # 分布には両方入る


def test_壊れたJSONでも落ちない():
    rows = [{**_row("approved", 80), "score_detail": "{壊れている"}]
    report = analyze(rows, WEIGHTS)
    assert report.no_detail == 1


def test_承認が無ければ何も言わない():
    """**材料が無いのに結論を出さない。**"""
    text = render(analyze([_row("rejected", 40)], WEIGHTS), WEIGHTS)
    assert "承認された物件がまだありません" in text
    assert "重みの案" not in text


def test_重みの案は合計1になる():
    rows = [
        _row("approved", 80, story=90, area=20, price=100),
        _row("rejected", 40, story=20, area=80, price=0),
    ]
    text = render(analyze(rows, WEIGHTS), WEIGHTS)
    assert "重みの案" in text
    # **案の表だけを読む。** 「軸ごとの効き方」にも同じ軸名が並ぶので、
    # 全体から拾うと素点まで足してしまう（最初にそれで 101 になった）。
    section = text.split("重みの案", 1)[1]
    values = []
    for line in section.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] in WEIGHTS:
            values.append(float(parts[2]))
    assert values, "案の表が読めていない"
    assert sum(values) == pytest.approx(1.0, abs=0.011)


def test_どの軸も差が無ければ重みの話にしない():
    """**重みをいじっても直らない**ことを、そう書く。"""
    rows = [_row("approved", 60, story=50, area=50, price=0),
            _row("rejected", 60, story=50, area=50, price=0)]
    text = render(analyze(rows, WEIGHTS), WEIGHTS)
    assert "どの軸も承認と非承認を分けていません" in text


def test_見せなかった候補は入らないと断る():
    """選択バイアス。ここを書かないと、足切りの根拠に誤用される。"""
    rows = [_row("approved", 80, story=90), _row("rejected", 40, story=10)]
    text = render(analyze(rows, WEIGHTS), WEIGHTS)
    assert "審査に上がらなかった候補" in text
