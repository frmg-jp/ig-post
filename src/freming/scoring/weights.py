"""軸ごとの点数を出し、config.yaml の重みで合算する。

LLMに任せるのは story だけで、残りの軸はこちらが持っている事実から
決める。ソースのランク、販売シグナルの検出結果、重点エリアの一覧は
すべて手元にあるので、推測させる理由がない。
"""

from __future__ import annotations

from freming.config import Config
from freming.db.connection import Row
from freming.scoring.schema import Assessment, ScoreAxis, ScoreResult
from freming.values import parse_year


def _area_axis(config: Config, assessment: Assessment, row: Row) -> ScoreAxis:
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


def _source_rank_axis(config: Config, row: Row) -> ScoreAxis:
    weight = config.scoring.weights.source_rank
    rank = row["source_rank"] or ""
    ratio = config.scoring.source_rank_score.get(rank, 0.0)
    return ScoreAxis("source", ratio * 100.0, weight, f"{row['source']}({rank or '不明'})")


def _for_sale_axis(config: Config, assessment: Assessment, row: Row) -> ScoreAxis:
    """販売中であることの確からしさ。

    収集時のシグナル検出（機械的）とLLMの読み（文脈）の両方を見る。
    両方が同意したときだけ満点にし、食い違うときは中間点にして
    審査UIで人が確認できるようにする。
    """
    weight = config.scoring.weights.editorial_for_sale_bonus
    # **仲介サイトにこの加点は与えない。** 名前のとおり「編集記事なのに
    # 実際に買える」ことへの加点で、物件情報サイトでは売り出し中が掲載の
    # 前提だから常に満点になる。台湾の物件が中身に関わらず審査に上がって
    # いた原因の一つがこの 20点の下駄だった。
    # 副作用として販売ソースの上限は 80点になり、highlight_above(80) には
    # 届かない。編集メディア発の候補を上に置く運用なので意図どおり。
    if config.listing_source(row["source"]):
        return ScoreAxis("for_sale", 0.0, weight, "販売サイト(加点対象外)")

    by_signal = bool(row["is_for_sale"])
    by_llm = assessment.is_for_sale

    if by_signal and by_llm:
        return ScoreAxis("for_sale", 100.0, weight, "シグナル/LLM一致")
    if by_signal or by_llm:
        which = "シグナルのみ" if by_signal else "LLMのみ"
        return ScoreAxis("for_sale", 50.0, weight, f"要確認({which})")
    return ScoreAxis("for_sale", 0.0, weight, "販売の裏付けなし")


def _price_axis(config: Config, assessment: Assessment, row: Row) -> ScoreAxis:
    """価格が判明しているか。金額の高低ではなく、掲載の有無を見る。

    価格が書かれていない記事は、売出中かどうかも曖昧なことが多い。
    高額であること自体を評価する意図はない。
    """
    weight = config.scoring.weights.price
    price = (assessment.price or row["price"] or "").strip()
    if not price:
        return ScoreAxis("price", 0.0, weight, "価格不明")
    return ScoreAxis("price", 100.0, weight, price[:20])


def _style_axis(config: Config, assessment: Assessment) -> ScoreAxis:
    """時代・様式が特定できるか。**承認実績でいちばん効いていた判定。**

    2026-09-04 の approval-report（審査済み106件）で、承認された65件の
    80% が真、非承認41件では 22%（差 +58pt）。他のどの軸よりも承認と
    非承認を分けていたのに、score_detail に書いているだけで点数には
    入れていなかった。

    真偽なので 0/100 の二値。中間点は作らない——「様式がなんとなく
    分かる」を作ると、結局 story と同じ連続値になって差が消える。
    """
    weight = config.scoring.weights.style_identified
    if assessment.style_identified:
        return ScoreAxis("style", 100.0, weight, assessment.style_name or "様式あり")
    return ScoreAxis("style", 0.0, weight, "様式不明")


def _one_of_a_kind_axis(config: Config, assessment: Assessment) -> ScoreAxis:
    """一点物か。同じく承認実績から（承認 72% / 非承認 32%、差 +41pt）。"""
    weight = config.scoring.weights.one_of_a_kind
    if assessment.one_of_a_kind:
        return ScoreAxis("one_of_a_kind", 100.0, weight, "一点物")
    return ScoreAxis("one_of_a_kind", 0.0, weight, "同一仕様が複数ありうる")


def build_result(
    config: Config, assessment: Assessment, row: Row, model: str
) -> ScoreResult:
    """LLMの判定と手元の事実から、最終スコアを組み立てる。"""
    weights = config.scoring.weights
    axes = [
        ScoreAxis("story", float(assessment.story_score), weights.story),
        _style_axis(config, assessment),
        _one_of_a_kind_axis(config, assessment),
        _source_rank_axis(config, row),
        _for_sale_axis(config, assessment, row),
        _genre_axis(config, assessment),
        _area_axis(config, assessment, row),
        _price_axis(config, assessment, row),
    ]
    return ScoreResult(assessment=assessment, axes=axes, model=model, gate=_gate(config, assessment))


def _waived(thresholds, assessment: Assessment) -> bool:
    """story の足切りを見送る判定が1つでも立っているか。

    名前は config が持つ（Assessment の属性名）。存在しない名前は
    config 読み込み時に弾いているので、ここでは素直に引く。
    """
    return any(getattr(assessment, name, False) for name in thresholds.story_min_waived_by)


def _gate(config: Config, assessment: Assessment) -> str:
    """加重合算の前に単独で落とす条件。該当すれば理由、しなければ空文字。

    story_score が低いものを落とすのは、加重平均に混ぜていたころ、
    中身の無い仲介物件が「販売中 + 重点エリア + 価格あり」の下駄だけで
    審査に上がっていたため。

    **ただし story だけで落とさない。** 2026-09-05 に未審査100件を
    採点し直したところ、様式も一点物性も満たしているのに story が
    40未満というだけで0点になる物件が出た（Victorian・一点物の
    2041 Pierce Street、Mediterranean・一点物の Malibu 邸など）。
    足切りは加重合算の前に効くので、承認実績でいちばん強かった2つの
    判定（style_identified +58pt / one_of_a_kind +41pt）が、story に
    上書きされて消えていた。

    story_min_waived_by に挙げた判定のどれかが真なら、story では
    落とさない。**どれも無いもの——物語も、様式も、一点物性も無い——
    だけが落ちる。**
    """
    thresholds = config.scoring.thresholds
    floor = thresholds.story_min
    if assessment.story_score < floor and not _waived(thresholds, assessment):
        return f"story={assessment.story_score} < {floor:.0f}"

    # 築年での足切り。収集の時点では築年が分からないのでここで見る。
    # 読み取れなかったものは落とさない——不明を落とすと、築年を書いて
    # いない良い記事まで消えるため。
    built_before = config.scoring.thresholds.built_before
    if built_before is not None:
        year = parse_year(assessment.year_built)
        if year is not None and year >= built_before:
            return f"築年 {year} ≧ {built_before}"
    return ""
