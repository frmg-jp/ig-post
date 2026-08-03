"""Claude API 呼び出し。

structured outputs（output_config.format）でJSONスキーマを渡し、
返答の形を保証する。パースの失敗をここで吸収し、上位には
Assessment だけを渡す。
"""

from __future__ import annotations

import json
import time

from freming.config import Config
from freming.logging_setup import get_logger
from freming.scoring.schema import OUTPUT_SCHEMA, Assessment

log = get_logger(__name__)


class ScoringError(RuntimeError):
    """スコアリングに失敗した。呼び出し側で1件スキップして続行する。"""


class ScoringClient:
    """1プロセスで使い回す。system プロンプトは全件で共通。"""

    def __init__(self, config: Config, system_prompt: str) -> None:
        import anthropic

        self.config = config
        self.system_prompt = system_prompt
        self.model = config.scoring.model
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def assess(self, user_prompt: str, max_attempts: int = 3) -> Assessment:
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return self._assess_once(user_prompt)
            except Exception as exc:  # 判定して再試行の可否を決める
                last_error = exc
                if not _is_retryable(exc):
                    # 鍵の不備やスキーマの誤りは、何度試しても同じ結果になる。
                    # 待ち時間をかけて全件失敗させるより、すぐ理由を出す。
                    raise ScoringError(f"再試行しても解決しない失敗です: {exc}") from exc
                if attempt == max_attempts:
                    break
                wait = 2.0 ** attempt
                log.warning(
                    "スコアリングに失敗（%d/%d）: %s — %.0f秒後に再試行",
                    attempt, max_attempts, exc, wait,
                )
                time.sleep(wait)
        raise ScoringError(f"{max_attempts}回試して判定できませんでした: {last_error}")

    def _output_config(self) -> dict:
        """リクエストの output_config を組み立てる。

        effort は全モデルにあるわけではない（Opus 4.5 以降と Sonnet 4.6 以降。
        Haiku 4.5 には無い）。非対応モデルに渡すと 400 になり、400 は
        `_is_retryable` が再試行しないので、その場で全件が失敗する。
        config で null にできるようにして、指定が無ければ送らない。
        """
        output_config: dict = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
        if self.config.scoring.effort:
            output_config["effort"] = self.config.scoring.effort
        return output_config

    def _assess_once(self, user_prompt: str) -> Assessment:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.config.scoring.max_tokens,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            output_config=self._output_config(),
        )
        _check_stop_reason(response, self.config.scoring.max_tokens)
        return Assessment.from_json(json.loads(_text_of(response)))


def _is_retryable(exc: Exception) -> bool:
    """再試行して解決する見込みがあるか。

    鍵の不備（401）、権限（403）、リクエストの誤り（400）は何度送っても
    同じ結果になる。3回×指数バックオフを費やしてから同じ理由で落ちるより、
    最初の1回で理由を返す。
    """
    import anthropic

    if isinstance(exc, ScoringError):
        return False
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    # 接続断・タイムアウトは時間をおけば直りうる
    return isinstance(exc, anthropic.APIConnectionError)


def _check_stop_reason(response, max_tokens: int) -> None:
    """本文を読む前に停止理由を確かめる。

    max_tokens で切れた返答は JSON として壊れており、パースの失敗として
    しか現れない。原因が分かる形で先に落とす。
    """
    reason = getattr(response, "stop_reason", None)
    if reason == "max_tokens":
        raise ScoringError(
            f"返答が max_tokens（{max_tokens}）で打ち切られました。"
            "config.yaml の scoring.max_tokens を増やすか effort を下げてください"
            "（thinking と本文は同じ上限を共有します）"
        )
    if reason == "refusal":
        raise ScoringError("モデルが判定を拒否しました（記事の内容を確認してください）")


_CHECK_ARTICLE = (
    "# 物件情報\n"
    "タイトル: Former fire station converted into a family home\n"
    "URL: https://example.com/check/\n\n"
    "# 記事本文\n"
    "The 1894 fire station retains its red arched engine doors, now the front "
    "entrance, and the original pressed tin ceiling. On the market for $1,250,000."
)


def check_api(config: Config) -> tuple[bool, str]:
    """API鍵とスキーマ契約が通ることを、1件分の小さな呼び出しで確かめる。

    まとめて採点し始めてから鍵の不備で全件失敗する、という事態を避ける。
    Drive の疎通確認（check-drive）と同じ役割。
    """
    try:
        # 値は使わない。未設定なら RuntimeError を投げるプロパティなので、
        # 読むこと自体が確認になる。捨てることを明示しておく。
        _ = config.anthropic_api_key
    except RuntimeError as exc:
        return False, str(exc)

    try:
        client = ScoringClient(config, "あなたは建築キュレーションメディアの編集者です。")
        assessment = client.assess(_CHECK_ARTICLE, max_attempts=1)
    except Exception as exc:  # noqa: BLE001 - 原因を問わず利用者に見せる
        return False, f"{config.scoring.model} を呼び出せませんでした: {exc}"

    return True, (
        f"疎通OK（model={config.scoring.model} "
        f"effort={config.scoring.effort or '送っていません'}）\n"
        f"  テスト記事の判定: genre={assessment.genre} "
        f"前歴={assessment.provenance_visible} story={assessment.story_score}\n"
        f"  {assessment.summary}"
    )


def _text_of(response) -> str:
    """返答から本文テキストを取り出す。thinking ブロックは読み飛ばす。"""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ScoringError("返答にテキストが含まれていません")
