"""[2] スコアリングの実行。

    未採点の候補 → Claude で判定 → 軸ごとに配点 → DBへ書き戻し

単体実行:
    python -m freming.scoring.runner [--limit 20] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

from freming.config import Config, load_config
from freming.db.connection import DbConnection, Row, connect
from freming.db.repository import (
    approved_rules,
    recent_reject_reasons,
    save_score,
    unscored_properties,
)
from freming.logging_setup import get_logger, setup_logging
from freming.scoring.client import ScoringClient, ScoringError
from freming.scoring.prompt import build_system_prompt, build_user_prompt
from freming.scoring.schema import ScoreResult
from freming.scoring.weights import build_result

log = get_logger(__name__)


@dataclass
class ScoreStats:
    scored: int = 0
    failed: int = 0
    below_threshold: int = 0
    highlighted: int = 0
    long_summaries: int = 0
    lines: list[str] = field(default_factory=list)

    def summary(self) -> str:
        line = (
            f"採点 {self.scored} 件（失敗 {self.failed} / "
            f"閾値未満 {self.below_threshold} / 注目 {self.highlighted}）"
        )
        if self.long_summaries:
            line += (
                f"\n  ※ {self.long_summaries} 件は summary が字数上限を超えています"
                "（config.yaml の scoring.summary_max_chars を確認してください）"
            )
        return line

    def report(self) -> str:
        return "\n".join(self.lines)


def _line(row: Row, result: ScoreResult) -> str:
    flags = []
    if result.assessment.provenance_visible:
        flags.append("前歴◎")
    if not result.assessment.is_for_sale:
        flags.append("売出?")
    mark = f" [{'/'.join(flags)}]" if flags else ""
    title = (row["title"] or row["source_url"])[:44]
    return (
        f"  {result.total:5.1f}  {title:<44}{mark}\n"
        f"         {' / '.join(a.line() for a in result.axes)}\n"
        f"         {row['source_url']}"
    )


def score_pending(
    config: Config,
    conn: DbConnection,
    limit: int | None = None,
    dry_run: bool = False,
    client: ScoringClient | None = None,
) -> ScoreStats:
    """未採点の候補をまとめて判定する。

    1件の失敗で全体を止めない。APIの一時的な失敗で他の候補まで
    採点されないままになる方が困るため。
    """
    stats = ScoreStats()
    rows = unscored_properties(conn, limit=limit or 50)
    if not rows:
        log.info("未採点の候補はありません")
        return stats

    if client is None:
        reasons = recent_reject_reasons(conn, config.scoring.feedback.recent_reasons_in_prompt)
        rules = approved_rules(conn)
        log.info(
            "直近の不承認理由 %d 件と、承認済みルール %d 件をプロンプトに含めます",
            len(reasons), len(rules),
        )
        client = ScoringClient(config, build_system_prompt(config, reasons, rules))

    thresholds = config.scoring.thresholds
    for row in rows:
        try:
            assessment = client.assess(build_user_prompt(row))
        except ScoringError as exc:
            log.error("判定できませんでした: %s (%s)", row["source_url"], exc)
            stats.failed += 1
            continue

        # 字数超過は切り詰めない。文の途中で切れた要約を納品するより、
        # 超えている事実を出して summary_max_chars か指示を見直す方がよい。
        limit = config.scoring.summary_max_chars
        if len(assessment.summary) > limit:
            log.warning(
                "summary が %d字を超えています（%d字）: %s",
                limit, len(assessment.summary), row["source_url"],
            )
            stats.long_summaries += 1

        result = build_result(config, assessment, row, client.model)
        stats.scored += 1
        if result.total < thresholds.min_to_persist:
            stats.below_threshold += 1
        if result.total >= thresholds.highlight_above:
            stats.highlighted += 1
        stats.lines.append(_line(row, result))

        if dry_run:
            continue
        save_score(
            conn,
            int(row["id"]),
            score=result.total,
            score_reason=result.reason(),
            score_detail=json.dumps(result.detail(), ensure_ascii=False),
            score_model=result.model,
            summary=assessment.summary,
            genre=assessment.genre if assessment.genre != "unknown" else None,
            architect=assessment.architect,
            year_built=assessment.year_built,
            city=assessment.city,
            country=assessment.country,
            price=assessment.price,
            provenance_visible=assessment.provenance_visible,
            usage_type=assessment.usage_type,
            structure=assessment.structure,
            building_area=assessment.building_area,
            site_area=assessment.site_area,
            style_name=assessment.style_name,
            summary_en=assessment.summary_en,
            photo_credit=assessment.photo_credit,
        )

    log.info(stats.summary())
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="未採点の候補をスコアリングする")
    parser.add_argument("--limit", type=int, default=None, help="採点する最大件数")
    parser.add_argument("--dry-run", action="store_true", help="DBに書き込まず結果だけ表示")
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.app.log_dir, config.app.log_level)
    conn = connect(config.app.target())
    try:
        stats = score_pending(config, conn, args.limit, args.dry_run)
    finally:
        conn.close()
    print(stats.summary())
    print(stats.report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
