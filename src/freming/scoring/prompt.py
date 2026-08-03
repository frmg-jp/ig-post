"""スコアリングのプロンプト生成。

承認基準は docs/approval-criteria.md にまとめた実例分析が根拠で、
その要点を config.yaml の scoring.approved_examples / approval_notes に
置いている。ここではそれをそのままプロンプトに載せる。基準を
コードに埋め込むと、基準を変えるたびに実装を触ることになるため。
"""

from __future__ import annotations


from freming.db.connection import Row
from freming.config import Config

_SYSTEM = """あなたは建築キュレーションメディア「FREMING CURATED」の編集者です。
記事を読んで、その物件を紹介する価値があるかを判定します。

# 判定の軸

{notes}

# これまで承認した物件（実例）

{examples}

# 直近で不承認にした理由

{rejects}

# 編集者が承認した恒久ルール

{rules}

# 守ること

- 記事に書かれていない事実を補わないこと。不明な項目は空文字にする。
- 「元○○を転用」と書かれているだけでは provenance_visible を true に
  しない。転用前の痕跡が残っていると読み取れる記述がある場合だけ true。
- is_for_sale は「いま売りに出ている」場合のみ true。建設費、落札額、
  過去の取引価格、周辺相場の話は該当しない。
- summary は日本語{summary_max_chars}字以内。誇張しない。
"""

_EMPTY = "（まだ蓄積がありません）"


def build_system_prompt(
    config: Config, reject_reasons: list[str], rules: list[str] | None = None
) -> str:
    """承認基準・直近の不承認理由・承認済みルールを1つのプロンプトにまとめる。

    rules は [7] 学習ループで人が承認したもの。繰り返された指摘だけが
    ここに昇格するので、直近の理由より強い指示として扱ってよい。
    """
    notes = "\n".join(f"{i}. {n}" for i, n in enumerate(config.scoring.approval_notes, 1))
    examples = "\n".join(f"- {e}" for e in config.scoring.approved_examples)
    return _SYSTEM.format(
        notes=notes or "（未設定）",
        examples=examples or "（未設定）",
        rejects="\n".join(f"- {r}" for r in reject_reasons) if reject_reasons else _EMPTY,
        rules="\n".join(f"- {r}" for r in (rules or [])) if rules else _EMPTY,
        summary_max_chars=config.scoring.summary_max_chars,
    )


def build_user_prompt(row: Row, max_chars: int = 6000) -> str:
    """1物件分の入力。収集時に保存した本文を使い、記事を再取得しない。"""
    text = (row["content_text"] or "").strip()
    truncated = text[:max_chars]
    if len(text) > max_chars:
        truncated += f"\n（以降 {len(text) - max_chars} 字は省略）"

    known = [
        f"タイトル: {row['title'] or '（なし）'}",
        f"URL: {row['source_url']}",
        f"取得元: {row['source']}（ランク {row['source_rank'] or '不明'}）",
    ]
    if row["price"]:
        known.append(f"収集時に検出した価格: {row['price']}")
    if row["for_sale_evidence"]:
        known.append(f"販売シグナル: {row['for_sale_evidence']}")

    return "\n".join(
        [
            "# 物件情報",
            *known,
            "",
            "# 記事本文",
            truncated or "（本文なし。タイトルとURLだけで判断してください）",
        ]
    )
