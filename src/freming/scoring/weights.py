"""軸ごとの点数を出し、config.yaml の重みで合算する。

LLMに任せるのは story だけで、残りの軸はこちらが持っている事実から
決める。ソースのランク、販売シグナルの検出結果、重点エリアの一覧は
すべて手元にあるので、推測させる理由がない。
"""

from __future__ import annotations

import sqlite3

from freming.config import Config
from freming.scoring.schema import Assessment, ScoreAxis, ScoreResult


def _area_axis(config: Config, assessment: Assessment, row: sqlite3.Row) -> ScoreAxis:
    """重点エリアとの一致。加点要素であり、外れても0点止まりで足切りはしない。

    承認実績8件のうち2件は当初エリア外だった（docs/approval-criteria.md）。
    エリアで候補を消さないために、ここでは減点をしない。
    """
    weight = config.scoring.weights.area_match
    city = (assessment.city or row["location_city"] or "").strip().lower()
    country = (assessment.country or row["location_country"] or "").strip().lower()

    if not city and not country:
        # 場所が分からないだけで落とさない。中間点にして他の軸に判断を委ねる。
        return ScoreAxis("area", 50.0, weight, "場所不明")

    for area in config.focus_areas:
        if area.city and area.city.lower() == city:
            for district in area.districts:
                if district.lower() in (assessment.provenance_note or "").lower():
                    return ScoreAxis("area", 100.0, weight, f"{area.city}/{district}")
            return ScoreAxis("area", 100.0, weight, area.city)
        if not area.city and area.country.lower() == country:
            return ScoreAxis("area", 100.0, weight, area.country)

    return ScoreAxis("area", 20.0, weight, f"重点エリア外({city or country})")


def _genre_axis(config: Config, assessment: Assessment) -> ScoreAxis:
    """優先ジャンルの順位で決める。priority の先頭ほど高い。"""
    weight = config.scoring.weights.genre_match
    priority = config.genres.priority
    genre = assessment.genre
    if genre not in priority:
        return ScoreAxis("genre", 20.0, weight, genre or "unknown")
    rank = priority.index(genre)
    # 先頭を100点、末尾を50点として等間隔に割り振る
    span = max(len(priority) - 1, 1)
    raw = 100.0 - (50.0 * rank / span)
    return ScoreAxis("genre", raw, weight, genre)


def _source_rank_axis(config: Config, row: sqlite3.Row) -> ScoreAxis:
    weight = config.scoring.weights.source_rank
    rank = row["source_rank"] or ""
    ratio = config.scoring.source_rank_score.get(rank, 0.0)
    return ScoreAxis("source", ratio * 100.0, weight, f"{row['source']}({rank or '不明'})")


def _for_sale_axis(config: Config, assessment: Assessment, row: sqlite3.Row) -> ScoreAxis:
    """販売中であることの確からしさ。

    収集時のシグナル検出（機械的）とLLMの読み（文脈）の両方を見る。
    両方が同意したときだけ満点にし、食い違うときは中間点にして
    審査UIで人が確認できるようにする。
    """
    weight = config.scoring.weights.editorial_for_sale_bonus
    by_signal = bool(row["is_for_sale"])
    by_llm = assessment.is_for_sale

    if by_signal and by_llm:
        return ScoreAxis("for_sale", 100.0, weight, "シグナル/LLM一致")
    if by_signal or by_llm:
        which = "シグナルのみ" if by_signal else "LLMのみ"
        return ScoreAxis("for_sale", 50.0, weight, f"要確認({which})")
    return ScoreAxis("for_sale", 0.0, weight, "販売の裏付けなし")


def _price_axis(config: Config, assessment: Assessment, row: sqlite3.Row) -> ScoreAxis:
    """価格が判明しているか。金額の高低ではなく、掲載の有無を見る。

    価格が書かれていない記事は、売出中かどうかも曖昧なことが多い。
    高額であること自体を評価する意図はない。
    """
    weight = config.scoring.weights.price
    price = (assessment.price or row["price"] or "").strip()
    if not price:
        return ScoreAxis("price", 0.0, weight, "価格不明")
    return ScoreAxis("price", 100.0, weight, price[:20])


def build_result(
    config: Config, assessment: Assessment, row: sqlite3.Row, model: str
) -> ScoreResult:
    """LLMの判定と手元の事実から、最終スコアを組み立てる。"""
    weights = config.scoring.weights
    axes = [
        ScoreAxis("story", float(assessment.story_score), weights.story),
        _source_rank_axis(config, row),
        _for_sale_axis(config, assessment, row),
        _genre_axis(config, assessment),
        _area_axis(config, assessment, row),
        _price_axis(config, assessment, row),
    ]
    return ScoreResult(assessment=assessment, axes=axes, model=model)
