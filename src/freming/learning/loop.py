"""[7] 学習ループ。

    非承認理由 → タグ分類 → 頻出タグをルール候補として提示 → 人が承認
                                                                    ↓
                            承認されたルールは次回スコアリングのプロンプトへ

ルールは自動適用しない。「3回同じ指摘が出た」ことと「今後それを恒久的に
除外してよい」ことは別なので、必ず人の承認を挟む。

単体実行:
    python -m freming.learning.loop [--limit 20]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field

from freming.config import Config, load_config
from freming.db.connection import connect
from freming.db.repository import (
    reasons_for_tag,
    set_feedback_tag,
    tag_counts,
    untagged_feedback,
    upsert_rule_candidate,
)
from freming.logging_setup import get_logger, setup_logging

log = get_logger(__name__)


@dataclass
class LearnStats:
    tagged: int = 0
    tagging_failed: int = 0
    new_candidates: list[str] = field(default_factory=list)
    updated_candidates: int = 0

    def summary(self) -> str:
        return (
            f"理由を分類 {self.tagged} 件（失敗 {self.tagging_failed}）/ "
            f"新しいルール候補 {len(self.new_candidates)} 件"
            f"（既存の更新 {self.updated_candidates}）"
        )


def _tagging_schema(tags: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["assignments"],
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "tag"],
                    "properties": {
                        "id": {"type": "integer"},
                        "tag": {"type": "string", "enum": tags},
                    },
                },
            }
        },
    }


_RULE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proposal"],
    "properties": {
        "proposal": {
            "type": "string",
            "description": "スコアリングのプロンプトにそのまま載せる1文の指示。断定的に、判定可能な形で書く。",
        }
    },
}


class LearningClient:
    """分類とルール文の生成。ScoringClient とは別のプロンプトを使う。"""

    def __init__(self, config: Config) -> None:
        import anthropic

        self.config = config
        self.model = config.scoring.model
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def _json(self, system: str, user: str, schema: dict) -> dict:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.config.scoring.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": self.config.scoring.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        for block in response.content:
            if getattr(block, "type", None) == "text":
                return json.loads(block.text)
        raise RuntimeError("返答にテキストが含まれていません")

    def classify(self, rows: list[sqlite3.Row], tags: list[str]) -> dict[int, str]:
        listing = "\n".join(f"{row['id']}: {row['reason']}" for row in rows)
        result = self._json(
            "あなたは建築キュレーションメディアの編集アシスタントです。"
            "編集者が物件を不承認にした理由を、決められたタグに分類します。"
            "どれにも当てはまらないものは other にしてください。",
            f"次の理由をそれぞれ分類してください。\n\n{listing}",
            _tagging_schema(tags),
        )
        return {int(a["id"]): a["tag"] for a in result.get("assignments", [])}

    def propose_rule(self, tag: str, reasons: list[str], hits: int) -> str:
        joined = "\n".join(f"- {r}" for r in reasons)
        result = self._json(
            "あなたは建築キュレーションメディアの編集者です。"
            "繰り返し不承認になっている理由から、今後の判定に使う指示を1文で書きます。",
            (
                f"タグ「{tag}」の理由が {hits} 件たまりました。\n{joined}\n\n"
                "これらに共通する判断基準を、スコアリングのプロンプトに載せる1文にしてください。"
                "「〜は対象外とする」「〜は story_score を下げる」のように、"
                "記事を読んで判定できる形で書いてください。"
            ),
            _RULE_SCHEMA,
        )
        return result["proposal"].strip()


def run_learning(
    config: Config,
    conn: sqlite3.Connection,
    limit: int | None = None,
    client: LearningClient | None = None,
) -> LearnStats:
    """未分類の理由にタグを付け、頻出タグをルール候補として登録する。"""
    stats = LearnStats()
    batch = limit or config.scoring.feedback.tagging_batch_size
    rows = untagged_feedback(conn, batch)

    if rows:
        client = client or LearningClient(config)
        try:
            assignments = client.classify(rows, config.scoring.feedback.tags)
        except Exception as exc:  # noqa: BLE001 - 分類の失敗でルール生成まで止めない
            log.error("理由の分類に失敗しました: %s", exc)
            assignments = {}
            stats.tagging_failed = len(rows)
        for row in rows:
            tag = assignments.get(int(row["id"]))
            if tag is None:
                continue
            set_feedback_tag(conn, int(row["id"]), tag)
            stats.tagged += 1
        conn.commit()
    else:
        log.info("未分類の非承認理由はありません")

    threshold = config.scoring.feedback.rule_candidate_min_hits
    for row in tag_counts(conn):
        if row["hits"] < threshold:
            continue
        tag = row["reason_tag"]
        client = client or LearningClient(config)
        try:
            proposal = client.propose_rule(
                tag, reasons_for_tag(conn, tag), int(row["hits"])
            )
        except Exception as exc:  # noqa: BLE001 - 1タグの失敗で全体を止めない
            log.error("ルール文の生成に失敗しました: tag=%s (%s)", tag, exc)
            continue
        if upsert_rule_candidate(conn, tag, int(row["hits"]), proposal):
            stats.new_candidates.append(f"{tag}（{row['hits']}件）: {proposal}")
        else:
            stats.updated_candidates += 1

    log.info(stats.summary())
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="非承認理由からルール候補を育てる")
    parser.add_argument("--limit", type=int, default=None, help="一度に分類する件数")
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.app.log_dir, config.app.log_level)
    conn = connect(config.app.db_path)
    try:
        stats = run_learning(config, conn, args.limit)
    finally:
        conn.close()
    print(stats.summary())
    for line in stats.new_candidates:
        print(f"  提案: {line}")
    if stats.new_candidates:
        print("\n採用するなら: python -m freming.cli rules approve <タグ>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
