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
        "usage_type",
        "structure",
        "building_area",
        "site_area",
        "style_name",
        "summary_en",
        "photo_credit",
        "display_name",
        "caption_body",
        "location_region",
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
            # 字数の上限は config.yaml の scoring.summary_max_chars が持ち、
            # システムプロンプト側で指示する。ここに数値を書くと二重管理になる。
            "description": "なぜ選ぶ価値があるかの一言（日本語）。",
        },
        "architect": {"type": "string", "description": "設計者。不明なら空文字。"},
        "year_built": {"type": "string", "description": "竣工年。不明なら空文字。"},
        "city": {"type": "string", "description": "都市名（英語表記）。不明なら空文字。"},
        "country": {"type": "string", "description": "国名（英語表記）。不明なら空文字。"},
        "price": {"type": "string", "description": "売出価格の原文表記。不明なら空文字。"},
        # --- 投稿キャプションの仕様欄に出す項目 ---
        # **推測させない。** 記事に書かれていなければ空文字にする。
        # 投稿の本文に事実として載るので、それらしい値を埋められると
        # 誤りがそのまま公開される。
        "usage_type": {
            "type": "string",
            "description": (
                "用途。英語で簡潔に（Private Residence / Historic Loft Residence /"
                " Apartment など）。記事から読み取れなければ空文字。"
            ),
        },
        "structure": {
            "type": "string",
            "description": (
                "構造・工法。英語で（Post-and-Beam / Wood Frame / Heavy Timber /"
                " Historic Brick Building など）。複数なら ' / ' で繋ぐ。"
                "記事に書かれていなければ空文字。"
            ),
        },
        "building_area": {
            "type": "string",
            "description": (
                "延床面積。**原文の単位と表記のまま**（'2,008 sq ft' / '187㎡'）。"
                "換算はしない。書かれていなければ空文字。"
            ),
        },
        "site_area": {
            "type": "string",
            "description": (
                "敷地面積。**原文の単位と表記のまま**（'0.82 Acres' / '15,000 sq ft'）。"
                "換算はしない。書かれていなければ空文字。"
            ),
        },
        "style_name": {
            "type": "string",
            "description": (
                "様式の名前。英語で（Mid-Century Modern / Spanish Colonial /"
                " Two-Story Industrial Loft など）。特定できなければ空文字。"
            ),
        },
        "summary_en": {
            "type": "string",
            "description": (
                "summary と同じ内容の英語版。1〜2文。日本語の直訳でなくてよいが、"
                "**書かれていない事実を足さない**。summary が空なら空文字。"
            ),
        },
        "display_name": {
            "type": "string",
            "description": (
                "投稿の見出しに使う短い物件名。英語。記事に固有名があればそれ"
                "（'Wade House' / 'The Benson House'）。無ければ年代・様式・種別"
                "から中立に組む（'1963 Mid-Century Residence' など）。記事に無い"
                "愛称を発明しない。組めなければ空文字。"
            ),
        },
        "caption_body": {
            "type": "string",
            "description": (
                "投稿の説明文。日本語、です・ます調、3〜6文（250〜450字目安）。"
                "設計者の文脈→素材・空間構成→履歴や現況、の順で記事の事実だけを"
                "書く。誇張しない。価格を書かない。記事に無い事実を足さない。"
                "人名・設計事務所名・建物名は原語（ラテン文字）のまま書き、"
                "カタカナに置き換えない。文が3つ作れないほど情報が薄ければ空文字。"
            ),
        },
        "location_region": {
            "type": "string",
            "description": (
                "州・地域（'California' / 'Connecticut' / 'Tuscany' など）。"
                "記事に書かれているときだけ。都市名・国名はここに入れない。"
                "無ければ空文字。"
            ),
        },
        "photo_credit": {
            "type": "string",
            "description": (
                "写真の撮影者。記事の 'Photography by ◯◯' / 'Photo: ◯◯' /"
                " 'Images courtesy of ◯◯' などから取る。**名前だけを返す**"
                "（'Photography by' は含めない）。書かれていなければ空文字。"
                "推測しないこと。"
            ),
        },
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
    usage_type: str = ""
    structure: str = ""
    building_area: str = ""
    site_area: str = ""
    style_name: str = ""
    summary_en: str = ""
    photo_credit: str = ""
    display_name: str = ""
    caption_body: str = ""
    location_region: str = ""

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
    # 空でなければ足切り理由。加重合算の結果に関わらず 0点にする。
    gate: str = ""

    @property
    def total(self) -> float:
        """足切りに掛かったものは、他の軸が何点でも0点。

        加重平均だけで判定していたころは、story が 0 でも「販売中」「重点
        エリア」「価格あり」の下駄だけで 54点に達し、min_to_persist(30) を
        楽に超えていた。物語性の無い物件を落とすには、平均に混ぜるのでは
        なく単独の条件として扱う必要がある。
        """
        if self.gate:
            return 0.0
        return round(sum(a.weighted for a in self.axes), 1)

    def reason(self) -> str:
        """score_reason に入れる、人が読んで納得できる説明。"""
        head = self.assessment.story_reason.strip()
        breakdown = " / ".join(a.line() for a in self.axes)
        body = f"{head}\n[内訳] {breakdown}" if head else f"[内訳] {breakdown}"
        return f"[足切り] {self.gate}\n{body}" if self.gate else body

    def detail(self) -> dict:
        return {
            "gate": self.gate,
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
