"""スコアリングでLLMに返させる構造化データの定義。

LLMには「抽出」と「物語性の判定」だけをさせ、最終スコアの合算は
Python 側で行う（scoring/weights.py）。理由は2つ。

  1. 重みは config.yaml で調整する値なので、変えるたびにAPIを呼び直す
     のは無駄。軸ごとの点数さえ保存しておけば再計算できる。
  2. ソースのランクや販売シグナルはこちらが既に持っている事実であり、
     LLMに推測させると誤りが混ざる。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# エリア・ジャンルは config.yaml 側で定義するが、LLMの出力は
# 「分からない」を許す。無理に埋めさせると誤った断定が入る。
GENRES = ["adaptive_reuse", "loft", "penthouse", "architect", "hidden_gem", "unknown"]

# structured outputs は厳格なスキーマを前提にしているため、全項目を required
# にして additionalProperties を閉じる。「分からない」は空文字で表す。
# 数値の範囲（minimum/maximum）はスキーマ側では指定せず、Python 側で丸める。
OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_for_sale",
        "genre",
        "provenance_visible",
        "provenance_note",
        "style_identified",
        "one_of_a_kind",
        "story_score",
        "story_reason",
        "summary",
        "architect",
        "year_built",
        "city",
        "country",
        "price",
    ],
    "properties": {
        "is_for_sale": {
            "type": "boolean",
            "description": "記事が『この物件が現在売りに出ている』ことを述べているか。建設費・落札額・過去の取引価格は該当しない。",
        },
        "genre": {"type": "string", "enum": GENRES},
        "provenance_visible": {
            "type": "boolean",
            "description": (
                "転用前の用途が視覚的に読み取れる状態で残っているか。"
                "壁面のゴーストサイン、用途固有の部材が現役（消防車扉、時計文字盤）、"
                "露出した木トラス・鋳鉄柱・レンガなど。単に『元倉庫』と書かれているだけで、"
                "痕跡が残っていない場合は false。"
            ),
        },
        "provenance_note": {
            "type": "string",
            "description": "前歴とその痕跡を1文で。該当しなければ空文字。",
        },
        "style_identified": {
            "type": "boolean",
            "description": "時代・様式が特定できるか（MCM、スパニッシュコロニアル、19世紀の産業建築など）。様式不明の現代建売は false。",
        },
        "one_of_a_kind": {
            "type": "boolean",
            "description": "一点物か。新築分譲やタワーマンションの1住戸のように同一仕様が複数あるものは false。",
        },
        "story_score": {
            "type": "integer",
            "description": "物語の強さを 0〜100 で。前歴の可視性を最重要、次に様式の特定性・一点物性・立地の主題性を見る。",
        },
        "story_reason": {
            "type": "string",
            "description": "story_score の根拠を1〜2文で。記事に書かれていた事実を挙げる。",
        },
        "summary": {
            "type": "string",
            "description": "なぜ選ぶ価値があるかの一言（日本語80字以内）。",
        },
        "architect": {"type": "string", "description": "設計者。不明なら空文字。"},
        "year_built": {"type": "string", "description": "竣工年。不明なら空文字。"},
        "city": {"type": "string", "description": "都市名（英語表記）。不明なら空文字。"},
        "country": {"type": "string", "description": "国名（英語表記）。不明なら空文字。"},
        "price": {"type": "string", "description": "売出価格の原文表記。不明なら空文字。"},
    },
}


@dataclass
class ScoreAxis:
    """1軸分の得点と根拠。重みを掛ける前の 0-100。"""

    key: str
    raw: float
    weight: float
    reason: str = ""

    @property
    def weighted(self) -> float:
        return self.raw * self.weight

    def line(self) -> str:
        return f"{self.key}={self.raw:.0f}×{self.weight:.2f}={self.weighted:.1f} {self.reason}".rstrip()


@dataclass
class Assessment:
    """LLMの返答をそのまま保持する。欠けている項目は空で埋める。"""

    is_for_sale: bool = False
    genre: str = "unknown"
    provenance_visible: bool = False
    provenance_note: str = ""
    style_identified: bool = False
    one_of_a_kind: bool = False
    story_score: int = 0
    story_reason: str = ""
    summary: str = ""
    architect: str = ""
    year_built: str = ""
    city: str = ""
    country: str = ""
    price: str = ""

    @classmethod
    def from_json(cls, data: dict) -> Assessment:
        known = set(cls.__dataclass_fields__)
        obj = cls(**{k: v for k, v in data.items() if k in known and v is not None})
        # 範囲はスキーマで縛っていないので、ここで丸める。
        obj.story_score = max(0, min(100, int(obj.story_score)))
        if obj.genre not in GENRES:
            obj.genre = "unknown"
        return obj


@dataclass
class ScoreResult:
    """1物件分のスコアリング結果。DBに保存する形。"""

    assessment: Assessment
    axes: list[ScoreAxis] = field(default_factory=list)
    model: str = ""

    @property
    def total(self) -> float:
        return round(sum(a.weighted for a in self.axes), 1)

    def reason(self) -> str:
        """score_reason に入れる、人が読んで納得できる説明。"""
        head = self.assessment.story_reason.strip()
        breakdown = " / ".join(a.line() for a in self.axes)
        return f"{head}\n[内訳] {breakdown}" if head else f"[内訳] {breakdown}"

    def detail(self) -> dict:
        return {
            "axes": [
                {"key": a.key, "raw": a.raw, "weight": a.weight, "reason": a.reason}
                for a in self.axes
            ],
            "flags": {
                "provenance_visible": self.assessment.provenance_visible,
                "style_identified": self.assessment.style_identified,
                "one_of_a_kind": self.assessment.one_of_a_kind,
                "llm_is_for_sale": self.assessment.is_for_sale,
            },
            "provenance_note": self.assessment.provenance_note,
        }
