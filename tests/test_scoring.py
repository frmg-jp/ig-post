"""[2] スコアリングのテスト。

Claude API は呼ばず、LLMの返答を差し替えて配点と保存を検証する。
「どんな判定が返ってきたら何点になるか」を固定しておかないと、
重みを変えたときに何が起きたのか分からなくなる。
"""

from __future__ import annotations

import json

import pytest

from freming.collect.base import Candidate
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import insert_candidate
from freming.scoring.prompt import build_system_prompt, build_user_prompt
from freming.scoring.runner import score_pending
from freming.scoring.schema import Assessment
from freming.scoring.weights import build_result


@pytest.fixture()
def config():
    return load_config("config.yaml")


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    connection = connect(path)
    yield connection
    connection.close()


def _add(conn, **overrides) -> int:
    data = {
        "source": "wowhaus",
        "source_rank": "A",
        "source_url": "https://example.com/loft/",
        "title": "Former warehouse in South Beach",
        "content_text": "A converted 1868 warehouse with exposed timber trusses. For sale at $2,400,000.",
        "for_sale_evidence": "for sale / $2,400,000",
        "signal_score": 2,
        "is_for_sale": 1,
        "price": None,
        "location_city": None,
        "location_country": None,
        "thumbnail_url": None,
    }
    data.update(overrides)
    property_id = insert_candidate(conn, Candidate(**data))
    conn.commit()
    return property_id


def _row(conn, property_id):
    return conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()


class FakeClient:
    """ScoringClient の代わり。返す Assessment を固定する。"""

    model = "fake-model"

    def __init__(self, assessment: Assessment) -> None:
        self.assessment = assessment
        self.prompts: list[str] = []

    def assess(self, user_prompt: str, **_kwargs) -> Assessment:
        self.prompts.append(user_prompt)
        return self.assessment


_STRONG = Assessment(
    is_for_sale=True,
    genre="adaptive_reuse",
    provenance_visible=True,
    provenance_note="1868年の倉庫。木トラスとゴーストサインが残る",
    style_identified=True,
    one_of_a_kind=True,
    story_score=90,
    story_reason="転用前の倉庫としての痕跡が残っている",
    summary="1868年の倉庫を住居に。木トラスがそのまま残る",
    city="San Francisco",
    country="United States",
    price="$2,400,000",
)


def test_strong_candidate_scores_high(config, conn) -> None:
    property_id = _add(conn)
    result = build_result(config, _STRONG, _row(conn, property_id), "test")
    assert result.total >= config.scoring.thresholds.highlight_above
    assert {a.key for a in result.axes} == {
        "story", "style", "one_of_a_kind",
        "source", "for_sale", "genre", "area", "price",
    }


def test_weighted_total_matches_config(config, conn) -> None:
    """合算がずれていないこと（重みは config.yaml の値をそのまま使う）。"""
    property_id = _add(conn)
    result = build_result(config, _STRONG, _row(conn, property_id), "test")
    expected = sum(a.raw * a.weight for a in result.axes)
    assert result.total == pytest.approx(round(expected, 1))


def test_area_outside_focus_is_penalised_but_not_zero(config, conn) -> None:
    """重点エリア外でも0点にはしない（物語性で通った実績があるため）。"""
    property_id = _add(conn)
    row = _row(conn, property_id)
    outside = Assessment(**{**_STRONG.__dict__, "city": "Antwerp", "country": "Belgium"})

    inside_axis = next(a for a in build_result(config, _STRONG, row, "t").axes if a.key == "area")
    outside_axis = next(a for a in build_result(config, outside, row, "t").axes if a.key == "area")

    assert inside_axis.raw == 100.0
    assert 0 < outside_axis.raw < inside_axis.raw


def test_unknown_location_does_not_punish(config, conn) -> None:
    """場所が分からないだけで不利にしない。"""
    property_id = _add(conn)
    row = _row(conn, property_id)
    unknown = Assessment(**{**_STRONG.__dict__, "city": "", "country": ""})
    axis = next(a for a in build_result(config, unknown, row, "t").axes if a.key == "area")
    assert axis.raw == 50.0


def test_for_sale_disagreement_is_flagged(config, conn) -> None:
    """収集時のシグナルとLLMの読みが食い違ったら満点にしない。"""
    property_id = _add(conn)
    row = _row(conn, property_id)          # is_for_sale = 1
    disagrees = Assessment(**{**_STRONG.__dict__, "is_for_sale": False})

    agree = next(a for a in build_result(config, _STRONG, row, "t").axes if a.key == "for_sale")
    differ = next(a for a in build_result(config, disagrees, row, "t").axes if a.key == "for_sale")

    assert agree.raw == 100.0
    assert differ.raw == 50.0
    assert "要確認" in differ.reason


def test_genre_priority_order_is_reflected(config, conn) -> None:
    """priority の先頭のジャンルほど高い点になること。"""
    property_id = _add(conn)
    row = _row(conn, property_id)
    first, last = config.genres.priority[0], config.genres.priority[-1]

    def genre_raw(genre: str) -> float:
        a = Assessment(**{**_STRONG.__dict__, "genre": genre})
        return next(x for x in build_result(config, a, row, "t").axes if x.key == "genre").raw

    assert genre_raw(first) > genre_raw(last) > genre_raw("unknown")


def test_score_is_persisted_with_breakdown(config, conn) -> None:
    """スコアだけでなく内訳と抽出属性も保存されること。"""
    property_id = _add(conn)
    stats = score_pending(config, conn, client=FakeClient(_STRONG))

    assert stats.scored == 1
    row = _row(conn, property_id)
    assert row["score"] is not None
    assert row["scored_at"] is not None
    assert row["score_model"] == "fake-model"
    assert row["genre"] == "adaptive_reuse"
    assert row["provenance_visible"] == 1
    assert row["location_city"] == "San Francisco"
    assert row["price"] == "$2,400,000"

    detail = json.loads(row["score_detail"])
    assert {a["key"] for a in detail["axes"]} == {
        "story", "style", "one_of_a_kind",
        "source", "for_sale", "genre", "area", "price",
    }
    assert detail["flags"]["provenance_visible"] is True
    # **0019 で列に出した2つ。** JSON の中だけに持っていたころは、
    # 絞り込みにも点数にも使えなかった。
    assert row["style_identified"] == 1
    assert row["one_of_a_kind"] == 1


def test_様式と一点物が点数に効く(config, conn) -> None:
    """**承認実績でいちばん効いていた2つ。** 0点でも通っていた。

    approval-report（審査済み106件）で style_identified は承認80%/非承認22%、
    one_of_a_kind は 72%/32%。どちらも score_detail に書くだけで、合算には
    入れていなかった。
    """
    property_id = _add(conn)
    row = _row(conn, property_id)
    weights = config.scoring.weights
    assert weights.style_identified > 0 and weights.one_of_a_kind > 0

    plain = Assessment(**{**_STRONG.__dict__,
                          "style_identified": False, "one_of_a_kind": False})
    strong = build_result(config, _STRONG, row, "t")
    weak = build_result(config, plain, row, "t")

    assert strong.total > weak.total
    # 二値。「なんとなく様式が分かる」を作ると story と同じ連続値になる。
    assert {a.raw for a in strong.axes if a.key in ("style", "one_of_a_kind")} == {100.0}
    assert {a.raw for a in weak.axes if a.key in ("style", "one_of_a_kind")} == {0.0}


def test_dry_run_does_not_write(config, conn) -> None:
    property_id = _add(conn)
    stats = score_pending(config, conn, dry_run=True, client=FakeClient(_STRONG))
    assert stats.scored == 1
    assert _row(conn, property_id)["score"] is None


def test_scored_rows_are_not_scored_again(config, conn) -> None:
    """再実行しても同じ候補にAPIを二度使わないこと。"""
    _add(conn)
    client = FakeClient(_STRONG)
    score_pending(config, conn, client=client)
    score_pending(config, conn, client=client)
    assert len(client.prompts) == 1


def test_one_failure_does_not_stop_the_rest(config, conn) -> None:
    from freming.scoring.client import ScoringError

    _add(conn, source_url="https://example.com/a/")
    _add(conn, source_url="https://example.com/b/")

    class Flaky(FakeClient):
        def assess(self, user_prompt: str, **kwargs):
            if "/a/" in user_prompt:
                raise ScoringError("一時的な失敗")
            return super().assess(user_prompt, **kwargs)

    stats = score_pending(config, conn, client=Flaky(_STRONG))
    assert stats.failed == 1
    assert stats.scored == 1


def test_system_prompt_carries_criteria_and_feedback(config, conn) -> None:
    """承認基準と直近の不承認理由がプロンプトに載ること。"""
    conn.execute(
        "INSERT INTO feedback (reason, created_at) VALUES (?, datetime('now'))",
        ("内装だけ新しく、元の用途の痕跡が残っていない",),
    )
    conn.commit()

    from freming.db.repository import recent_reject_reasons

    prompt = build_system_prompt(config, recent_reject_reasons(conn, 30))
    assert config.scoring.approved_examples[0][:20] in prompt
    assert config.scoring.approval_notes[0][:20] in prompt
    assert "痕跡が残っていない" in prompt


def test_user_prompt_uses_stored_text_and_truncates(config, conn) -> None:
    """記事を取り直さず、収集時の本文を使うこと。"""
    property_id = _add(conn, content_text="x" * 9000)
    prompt = build_user_prompt(_row(conn, property_id), max_chars=100)
    assert "https://example.com/loft/" in prompt
    assert "省略" in prompt
    assert len(prompt) < 1000


def test_assessment_clamps_out_of_range_values() -> None:
    """スキーマで縛らない値は Python 側で丸めること。"""
    assert Assessment.from_json({"story_score": 150}).story_score == 100
    assert Assessment.from_json({"story_score": -5}).story_score == 0
    assert Assessment.from_json({"genre": "castle"}).genre == "unknown"


def test_output_schema_is_strict() -> None:
    """structured outputs 用に、全項目 required・追加項目なしであること。"""
    from freming.scoring.schema import OUTPUT_SCHEMA

    assert OUTPUT_SCHEMA["additionalProperties"] is False
    assert set(OUTPUT_SCHEMA["required"]) == set(OUTPUT_SCHEMA["properties"])


def test_check_api_reports_missing_key(config, monkeypatch) -> None:
    """鍵が無いときは、採点を始める前に理由が分かること。"""
    from freming.scoring.client import check_api

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, message = check_api(config)
    assert ok is False
    assert "ANTHROPIC_API_KEY" in message


def test_truncated_response_reports_max_tokens(config) -> None:
    """max_tokens で切れた返答を、JSONパース失敗ではなく原因として出すこと。"""
    from freming.scoring.client import ScoringError, _check_stop_reason

    class _Resp:
        stop_reason = "max_tokens"

    with pytest.raises(ScoringError) as exc:
        _check_stop_reason(_Resp(), 2000)
    assert "max_tokens" in str(exc.value)


def test_refusal_is_reported(config) -> None:
    from freming.scoring.client import ScoringError, _check_stop_reason

    class _Resp:
        stop_reason = "refusal"

    with pytest.raises(ScoringError):
        _check_stop_reason(_Resp(), 8000)


def test_normal_stop_reason_passes(config) -> None:
    from freming.scoring.client import _check_stop_reason

    class _Resp:
        stop_reason = "end_turn"

    _check_stop_reason(_Resp(), 8000)   # 例外が出ないこと


def test_non_retryable_errors_are_not_retried() -> None:
    """鍵の不備やリクエストの誤りを3回試さないこと。

    同じ理由で失敗すると分かっているものに指数バックオフを費やすと、
    全件が失敗するまでの時間が伸びるだけになる。
    """
    import anthropic
    import httpx

    from freming.scoring.client import _is_retryable

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def _status_error(status: int) -> anthropic.APIStatusError:
        return anthropic.APIStatusError(
            "err", response=httpx.Response(status, request=request), body=None
        )

    assert _is_retryable(_status_error(429)) is True
    assert _is_retryable(_status_error(500)) is True
    assert _is_retryable(_status_error(401)) is False
    assert _is_retryable(_status_error(400)) is False
    assert _is_retryable(anthropic.APIConnectionError(request=request)) is True


def test_max_tokens_leaves_room_for_thinking(config) -> None:
    """claude-sonnet-5 は thinking が既定で動くため、上限に余裕が要る。

    max_tokens は thinking と本文の合計に効く。判定結果のJSONが小さくても
    2000 程度では打ち切られうるので、設定側で余裕を確保しておく。
    """
    assert config.scoring.max_tokens >= 4000


def test_summary_limit_comes_from_config(config) -> None:
    """summary の字数上限が config.yaml から効くこと。

    以前は 80 という数値がプロンプトとスキーマに直書きされていて、
    scoring.summary_max_chars を変えても何も起きなかった。
    """
    cfg = config.model_copy(deep=True)
    cfg.scoring.summary_max_chars = 120

    assert "120字以内" in build_system_prompt(cfg, [])
    assert "80字以内" not in build_system_prompt(cfg, [])


def test_long_summary_is_reported_not_truncated(config, conn) -> None:
    """字数超過は切り詰めず、超えている事実を報告すること。"""
    property_id = _add(conn)
    long_summary = "あ" * (config.scoring.summary_max_chars + 20)
    verbose = Assessment(**{**_STRONG.__dict__, "summary": long_summary})

    stats = score_pending(config, conn, client=FakeClient(verbose))

    assert stats.long_summaries == 1
    assert "字数上限" in stats.summary()
    # 切り詰めずにそのまま保存する（途中で切れた要約を納品しない）
    assert _row(conn, property_id)["summary"] == long_summary


# ----------------------------------------------------------------------
# 重点エリア（2026-08-03: アメリカを全土に変更）
# ----------------------------------------------------------------------
def _area(cfg, city: str, country: str):
    from freming.scoring.schema import Assessment
    from freming.scoring.weights import _area_axis

    class _Row(dict):
        def __getitem__(self, key):
            return self.get(key)

    return _area_axis(cfg, Assessment(city=city, country=country), _Row())


def test_any_us_city_is_a_focus_area() -> None:
    """都市指定をやめたので、掲載されていなかった都市も重点になる。"""
    cfg = load_config("config.yaml")
    for city in ("Austin", "Marfa", "Detroit", "Providence"):
        assert _area(cfg, city, "United States").raw == 100.0


def test_previously_listed_us_cities_still_score_full() -> None:
    """変更前から重点だった都市が下がっていないこと。"""
    cfg = load_config("config.yaml")
    for city in ("Los Angeles", "New York", "Chicago", "Seattle"):
        assert _area(cfg, city, "United States").raw == 100.0


def test_other_focus_countries_are_unchanged() -> None:
    cfg = load_config("config.yaml")
    for city, country in (("Lisbon", "Portugal"), ("Alicante", "Spain"), ("Taipei", "Taiwan")):
        assert _area(cfg, city, country).raw == 100.0


def test_outside_the_focus_areas_is_not_zero() -> None:
    """エリアで候補を消さない。承認実績8件のうち2件は当初エリア外だった。"""
    cfg = load_config("config.yaml")
    axis = _area(cfg, "Provence", "France")
    assert axis.raw == 20.0
    assert "重点エリア外" in axis.reason


# --- リクエストの組み立て -------------------------------------------------
#
# effort は全モデルにあるパラメータではない（Opus 4.5 以降と Sonnet 4.6
# 以降のみ）。非対応モデルに送ると 400 になり、400 は再試行されないので
# その場で全件が失敗する。config で外せることを固定しておく。


def _client(monkeypatch, effort):
    from freming.scoring.client import ScoringClient

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cfg = load_config("config.yaml")
    cfg.scoring.effort = effort
    return ScoringClient(cfg, "system")


def test_effort_is_not_sent_when_it_is_unset(monkeypatch) -> None:
    output_config = _client(monkeypatch, None)._output_config()
    assert "effort" not in output_config
    assert output_config["format"]["type"] == "json_schema"


def test_effort_is_sent_when_configured(monkeypatch) -> None:
    assert _client(monkeypatch, "medium")._output_config()["effort"] == "medium"


def test_the_configured_model_does_not_use_effort() -> None:
    """config.yaml の model と effort の組み合わせが噛み合っていること。

    effort を持たないモデルに effort を残したままにすると、採点が
    まるごと落ちる。設定ファイル側の取り違えをここで止める。
    """
    cfg = load_config("config.yaml")
    without_effort = ("claude-haiku-4-5", "claude-3-5-haiku", "claude-haiku-3")
    if cfg.scoring.model.startswith(without_effort):
        assert cfg.scoring.effort is None, (
            f"{cfg.scoring.model} は effort を受け付けません。config.yaml で null にしてください"
        )


# ----------------------------------------------------------------------
# 足切りと、販売ソースへの for_sale 加点の扱い。
#
# 台湾の仲介物件が中身に関係なく審査に上がっていた回帰を防ぐ。原因は
# 加重平均そのもので、story=0 でも「販売中20 + 重点エリア20 + ランクB6 +
# 価格5 + ジャンル不明3」の54点が積み上がり、min_to_persist(30) を
# 楽に超えていた。


_WEAK = Assessment(
    is_for_sale=True,
    genre="unknown",
    provenance_visible=False,
    style_identified=False,
    one_of_a_kind=False,
    story_score=0,
    story_reason="標準的な分譲マンションで建築的な特徴がない",
    summary="標準的な分譲マンション",
    city="Taipei",
    country="Taiwan",
    price="1,488萬",
)


def _listing_row(conn, source="hbhousing"):
    property_id = _add(
        conn,
        source=source,
        source_rank="B",
        source_url=f"https://example.com/{source}/1",
        location_country="Taiwan",
        price="1,488萬",
    )
    return _row(conn, property_id)


def test_no_story_is_gated_to_zero(config, conn) -> None:
    """物語性が無いものは、他の軸が何点でも0点で落ちる。"""
    result = build_result(config, _WEAK, _listing_row(conn), "test")
    assert result.gate
    assert result.total == 0.0
    assert result.total < config.scoring.thresholds.min_to_persist
    # 内訳は残す。なぜ落ちたのかを審査UIで追えるようにするため。
    assert "足切り" in result.reason()


def test_gate_boundary_follows_config(config, conn) -> None:
    floor = int(config.scoring.thresholds.story_min)
    row = _listing_row(conn)
    below = build_result(config, Assessment(**{**_WEAK.__dict__, "story_score": floor - 1}), row, "t")
    at = build_result(config, Assessment(**{**_WEAK.__dict__, "story_score": floor}), row, "t")
    assert below.total == 0.0
    assert at.total > 0.0


def test_listing_source_gets_no_for_sale_bonus(config, conn) -> None:
    """仲介サイトは「売り出し中」が掲載の前提なので加点しない。

    編集記事に与える加点をそのまま渡すと、物件の良し悪しと無関係に
    20点の下駄になる。
    """
    strong_on_listing = Assessment(**{**_STRONG.__dict__, "country": "Taiwan"})
    result = build_result(config, strong_on_listing, _listing_row(conn), "test")
    for_sale = next(a for a in result.axes if a.key == "for_sale")
    assert for_sale.raw == 0.0
    assert "加点対象外" in for_sale.reason


def test_editorial_source_keeps_for_sale_bonus(config, conn) -> None:
    """編集メディア側の加点はこれまでどおり効く（降格させていない）。"""
    property_id = _add(conn)  # wowhaus = editorial_sources
    result = build_result(config, _STRONG, _row(conn, property_id), "test")
    for_sale = next(a for a in result.axes if a.key == "for_sale")
    assert for_sale.raw == 100.0


# ----------------------------------------------------------------------
# 築年での足切り。
#
# 「2000年築より新しいものは省く」。収集の時点では築年が分からないので、
# 採点で取れた year_built を見る。


def test_built_in_2000_or_later_is_gated(config, conn) -> None:
    property_id = _add(conn)
    row = _row(conn, property_id)
    for year in ("2000", "2015", "built in 2021"):
        assessment = Assessment(**{**_STRONG.__dict__, "year_built": year})
        result = build_result(config, assessment, row, "test")
        assert result.total == 0.0, year
        assert "築年" in result.gate


def test_built_before_the_line_still_passes(config, conn) -> None:
    property_id = _add(conn)
    row = _row(conn, property_id)
    for year in ("1868", "1999", "c. 1920s"):
        assessment = Assessment(**{**_STRONG.__dict__, "year_built": year})
        result = build_result(config, assessment, row, "test")
        assert result.total > 0.0, year
        assert not result.gate


def test_unknown_year_is_not_dropped(config, conn) -> None:
    """築年を書いていない良い記事まで消さない。"""
    property_id = _add(conn)
    assessment = Assessment(**{**_STRONG.__dict__, "year_built": ""})
    result = build_result(config, assessment, _row(conn, property_id), "test")
    assert result.total > 0.0
    assert not result.gate


def test_the_year_line_comes_from_config(config, conn) -> None:
    assert config.scoring.thresholds.built_before == 2000
