"""[7] 学習ループのテスト。

Claude API は呼ばず、分類結果とルール文を差し替える。
確かめたいのは「人の判断を経ずにルールが適用されないこと」。
"""

from __future__ import annotations

import pytest

from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import (
    approved_rules,
    decide_rule_candidate,
    list_rule_candidates,
    recent_reject_reasons,
)
from freming.learning.loop import run_learning
from freming.scoring.prompt import build_system_prompt


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "learn.db"
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


def _feedback(conn, reason: str, tag: str | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO feedback (reason, reason_tag, created_at) VALUES (?, ?, datetime('now'))",
        (reason, tag),
    )
    conn.commit()
    return int(cursor.lastrowid)


class FakeClient:
    """LearningClient の代わり。全件を同じタグに寄せる。"""

    def __init__(self, tag: str = "no_visible_provenance", proposal: str = "痕跡が残っていない物件は対象外とする") -> None:
        self.tag = tag
        self.proposal = proposal
        self.classify_calls = 0
        self.propose_calls = 0

    def classify(self, rows, tags):
        self.classify_calls += 1
        assert self.tag in tags
        return {int(r["id"]): self.tag for r in rows}

    def propose_rule(self, tag, reasons, hits):
        self.propose_calls += 1
        return self.proposal


def test_untagged_reasons_get_classified(config, conn) -> None:
    ids = [_feedback(conn, f"理由{i}") for i in range(3)]
    stats = run_learning(config, conn, client=FakeClient())

    assert stats.tagged == 3
    tags = conn.execute(
        "SELECT reason_tag FROM feedback WHERE id IN (?, ?, ?)", ids
    ).fetchall()
    assert {t["reason_tag"] for t in tags} == {"no_visible_provenance"}


def test_rule_candidate_appears_at_threshold(config, conn) -> None:
    """同じ指摘が閾値に達したらルール候補として提示されること。"""
    threshold = config.scoring.feedback.rule_candidate_min_hits
    for i in range(threshold):
        _feedback(conn, f"痕跡が残っていない {i}")

    stats = run_learning(config, conn, client=FakeClient())

    assert len(stats.new_candidates) == 1
    row = list_rule_candidates(conn, "proposed")[0]
    assert row["reason_tag"] == "no_visible_provenance"
    assert row["hit_count"] == threshold


def test_below_threshold_produces_no_candidate(config, conn) -> None:
    _feedback(conn, "痕跡が残っていない")
    stats = run_learning(config, conn, client=FakeClient())
    assert stats.new_candidates == []
    assert list_rule_candidates(conn) == []


def test_candidates_are_not_applied_until_approved(config, conn) -> None:
    """提案されただけのルールはプロンプトに載らないこと。"""
    for i in range(config.scoring.feedback.rule_candidate_min_hits):
        _feedback(conn, f"痕跡が残っていない {i}")
    run_learning(config, conn, client=FakeClient())

    assert approved_rules(conn) == []
    prompt = build_system_prompt(config, [], approved_rules(conn))
    assert "痕跡が残っていない物件は対象外とする" not in prompt


def test_approved_rule_reaches_the_prompt(config, conn) -> None:
    """人が承認したら次回のスコアリングに効くこと（ループが閉じる）。"""
    for i in range(config.scoring.feedback.rule_candidate_min_hits):
        _feedback(conn, f"痕跡が残っていない {i}")
    run_learning(config, conn, client=FakeClient())

    assert decide_rule_candidate(conn, "no_visible_provenance", "approved")

    rules = approved_rules(conn)
    assert rules == ["痕跡が残っていない物件は対象外とする"]
    assert "痕跡が残っていない物件は対象外とする" in build_system_prompt(config, [], rules)


def test_dismissed_candidate_is_not_proposed_again(config, conn) -> None:
    """一度却下したものを件数が増えても蒸し返さないこと。"""
    for i in range(config.scoring.feedback.rule_candidate_min_hits):
        _feedback(conn, f"理由 {i}")
    run_learning(config, conn, client=FakeClient())
    decide_rule_candidate(conn, "no_visible_provenance", "dismissed")

    _feedback(conn, "さらに1件")
    stats = run_learning(config, conn, client=FakeClient())

    assert stats.new_candidates == []
    assert list_rule_candidates(conn, "dismissed")[0]["reason_tag"] == "no_visible_provenance"


def test_other_tag_never_becomes_a_rule(config, conn) -> None:
    """other は雑多な理由の受け皿なので、まとめてルールにしない。"""
    for i in range(config.scoring.feedback.rule_candidate_min_hits + 2):
        _feedback(conn, f"雑多な理由 {i}", tag="other")

    stats = run_learning(config, conn, client=FakeClient())
    assert stats.new_candidates == []


def test_classification_failure_does_not_lose_reasons(config, conn) -> None:
    """分類に失敗しても理由そのものは残り、次回やり直せること。"""

    class Broken(FakeClient):
        def classify(self, rows, tags):
            raise RuntimeError("APIエラー")

    _feedback(conn, "痕跡が残っていない")
    stats = run_learning(config, conn, client=Broken())

    assert stats.tagged == 0
    assert stats.tagging_failed == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM feedback WHERE reason_tag IS NULL"
    ).fetchone()["n"] == 1


def test_reasons_reach_the_prompt_even_before_tagging(config, conn) -> None:
    """タグが付く前でも、直近の理由はスコアリングに渡ること。"""
    _feedback(conn, "内装だけ新しく、元の用途の痕跡が残っていない")
    prompt = build_system_prompt(config, recent_reject_reasons(conn, 30))
    assert "痕跡が残っていない" in prompt


def test_tags_must_contain_other(config) -> None:
    """どのタグにも当てはまらない理由の受け皿を必須にする。"""
    from pydantic import ValidationError

    from freming.config import FeedbackConfig

    with pytest.raises(ValidationError):
        FeedbackConfig(tags=["style_unclear"])
