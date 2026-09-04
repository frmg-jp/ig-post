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


def test_承認された物件を現物で並べられる():
    """プロンプトの実例（approved_examples）を書き直すための材料。

    **集計では代わりにならない。** ジャンルの内訳を見ても、実例の文面は
    書けない。既定では出さず、件数を指定したときだけ出す。
    """
    rows = [
        {**_row("approved", 82), "display_name": "Eagle Warehouse",
         "architect": "Frank Freeman", "style_name": "Romanesque Revival",
         "summary": "時計文字盤がそのまま窓", "year_built": "1893"},
        {**_row("approved", 60), "display_name": "Desert House",
         "architect": "", "style_name": "", "summary": "岩塊の直下"},
        {**_row("rejected", 90), "display_name": "出てはいけない"},
    ]
    report = analyze(rows, WEIGHTS)

    assert [e.name for e in report.examples] == ["Eagle Warehouse", "Desert House"]

    # 「承認された物件」だけでは軸の説明文にも当たるので、見出しで見る。
    assert "点数の高い順に" not in render(report, WEIGHTS)          # 既定では出さない
    text = render(report, WEIGHTS, examples=1)
    assert "Eagle Warehouse" in text and "Frank Freeman" in text
    assert "Desert House" not in text        # 点数の高い順に1件だけ
    assert "出てはいけない" not in text      # 非承認は入らない


def test_判定が列に入っていなければ知らせる():
    """**0019 の埋め戻しが効いたかを、これで確かめる。**

    上の割合は score_detail の JSON から数えているので、列が空でも
    表は普通に出てしまう。列が埋まっていなければ審査UIの絞り込みには
    使えないので、件数を出して区別する。
    """
    filled = {"provenance_visible": 1, "style_identified": 1, "one_of_a_kind": 0}
    rows = [{**_row("approved", 80), **filled},
            {**_row("rejected", 40), **filled, "style_identified": None}]
    report = analyze(rows, WEIGHTS)
    assert report.columns_filled["style_identified"] == 1   # 2件中1件
    assert report.columns_filled["provenance_visible"] == 2

    text = render(report, WEIGHTS)
    assert "列に入っていない判定がある" in text
    assert "style_identified" in text.split("列に入っていない判定がある", 1)[1]

    # 全部埋まっていれば黙る
    full = [{**_row("approved", 80), **filled},
            {**_row("rejected", 40), **filled}]
    assert "列に入っていない判定がある" not in render(analyze(full, WEIGHTS), WEIGHTS)


def test_実例用の列が無い行でも落ちない():
    """集計だけを使う呼び出しが、実例用の列まで揃えなくて済むこと。"""
    report = analyze([_row("approved", 80)], WEIGHTS)
    assert report.examples[0].name == "#1"   # display_name も title も無い


def test_足切りの理由を種類ごとに分ける():
    """**どちらの足切りを見直すのかは、理由の内訳を見ないと決まらない。**

    件数だけでは story_min が厳しいのか built_before が厳しいのか
    区別できず、片方を動かしても外れる。
    """
    rows = [
        _row("approved", 0, gate="story=25 < 40"),
        _row("approved", 0, gate="story=35 < 40"),
        _row("approved", 0, gate="築年 2021 ≧ 2000"),
        _row("rejected", 0, gate="story=10 < 40"),   # 非承認は数えない
    ]
    report = analyze(rows, WEIGHTS)
    assert report.approved_gated == 3
    assert report.approved_gate_kinds["story_min"] == 2
    assert report.approved_gate_kinds["built_before"] == 1
    assert report.approved_gated_story == [25.0, 35.0]

    text = render(report, WEIGHTS)
    assert "story_min 2件" in text
    assert "built_before 1件" in text
    # 下限をいくつまで下げれば拾えたのかが読めること
    assert "25/35" in text


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


def test_比較材料の無い軸の重みは案から取り上げない():
    """**足したばかりの軸を毎回0にする案を出さないこと。**

    style/one_of_a_kind を軸に足した直後、既存の score_detail にその軸は
    無い（過去の採点が書いていない）。案を全体で1.0に正規化していたころ、
    比較できる軸だけで1.0を配り直してしまい、新しい軸の重みを毎回
    まるごと取り上げる案になっていた。
    """
    # 内訳に出る軸（story/area/price）と、足したばかりの軸だけにする。
    weights = {"story": 0.30, "area": 0.20, "price": 0.10,
               "style": 0.20, "one_of_a_kind": 0.20}
    rows = [_row("approved", 80, story=90, area=20),
            _row("rejected", 40, story=20, area=80)]
    section = render(analyze(rows, weights), weights).split("重みの案", 1)[1]

    values = [float(p[2]) for p in (l.split() for l in section.splitlines())
              if len(p) >= 3 and p[0] in weights]
    assert values, "案の表が読めていない"
    # 比較できる3軸が持っていた 0.60 の中で配り直す（1.0 ではない）
    assert sum(values) == pytest.approx(0.60, abs=0.011)
    assert "style 0.20" in section and "one_of_a_kind 0.20" in section
    assert "比較材料がありません" in section


def test_重みの案は比較できる軸の合計に収まる():
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
    # 内訳に出るのは story/area/price の3軸だけなので、案もその3軸が
    # 持っていた 0.25+0.20+0.05 の中に収まる。
    assert sum(values) == pytest.approx(0.50, abs=0.011)


def test_どの軸も差が無ければ重みの話にしない():
    """**重みをいじっても直らない**ことを、そう書く。"""
    rows = [_row("approved", 60, story=50, area=50, price=0),
            _row("rejected", 60, story=50, area=50, price=0)]
    text = render(analyze(rows, WEIGHTS), WEIGHTS)
    assert "どの軸も承認と非承認を分けていません" in text


def test_採点まで届かなかった候補は入らないと断る():
    """選択バイアス。**ただし「低得点は審査に出ない」ではない。**

    min_to_persist は採点ログの件数を数えているだけで、保存も一覧も
    止めていない（list_properties の min_score は誰も渡していない）。
    そう書くと、足切りに掛かった候補が人の目に触れていないことになり、
    実績を読み違える。
    """
    rows = [_row("approved", 80, story=90), _row("rejected", 40, story=10)]
    text = render(analyze(rows, WEIGHTS), WEIGHTS)
    assert "収集が拾わなかった物件は入っていません" in text
    assert "min_to_persist" not in text
