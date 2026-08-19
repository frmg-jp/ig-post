"""[9] 投稿の本文を組む。

型は実際に運用している @frmg.jpn の投稿4本から起こした（2026-08-19 受領。
それ以前は 2026-08-07 の3本が根拠だった）:

    リード（日英2行）
    【 物件名 】                        ← display_name（無ければ title）
    仕様欄（Location / Usage / Architect / Building Area / Site Area / Built in）
    説明文（日本語のみ・複数文）        ← caption_body（無ければ summary）
    Photo: ◯◯                          ← 型に無いが意図して足している（2026-08-08）
    物件詳細はストーリーズ・ハイライトへ ほか3行
    Sampling × Renovation / CURATED BY FREMING
    【 お気軽にご相談ください 】＋4事業
    ※This post is curated based on …（英文の注記）
    ハッシュタグ（1行・空白区切り）

文言は config.yaml の `caption` が持つ。ここは並べ方だけを持つ。

守っていること:

  - **値が無い行は出さない。** 「Architect: 不明」のような行を出すより、
    その行ごと落とす。投稿は事実の記載なので、埋めるために推測しない
  - **価格は載せない。** 実物の投稿にも入っていない。通貨が混ざるうえ
    為替で見え方が変わり、成約後も直せない
  - **写真の出所は書く。** 編集メディアの写真を使わせてもらう以上、
    撮影者（取れなければ媒体名）を1行入れる
  - リンクも載せない。キャプション内のURLはリンクにならない
  - **音源のクレジットは、上限で切るときも必ず残す。** CC BY は表記が
    要件で、落とすとライセンス違反になる
"""

from __future__ import annotations

import re

from freming.config import CaptionConfig
from freming.db.connection import Row

# Instagram のキャプションの上限。超えると投稿ごと弾かれる。
MAX_LENGTH = 2200


def _get(row: Row, key: str) -> str:
    """Row から安全に取り出す。列が無ければ空文字。"""
    try:
        value = row[key]
    except (KeyError, IndexError):
        return ""
    return str(value).strip() if value else ""


def _place(row: Row) -> str:
    city = _get(row, "location_city")
    country = _get(row, "location_country")
    if city and country:
        return f"{city}, {country}"
    return city or country


def _spec_lines(row: Row, config: CaptionConfig) -> list[str]:
    """仕様欄。値のあるものだけを設定した順に並べる。"""
    lines = []
    for key, label in config.spec:
        value = _place(row) if key == "location" else _get(row, key)
        if value:
            lines.append(f"{label}: {value}")
    return lines


# Instagram のタグ数上限。超えるとタグとして効かなくなる。
MAX_HASHTAGS = 30

# 国 → 「◯◯建築」タグの形容詞。実運用の投稿は #BritishArchitecture の形で
# 使っている。USA は州のタグ（#CaliforniaArchitecture）で表すので入れない。
_COUNTRY_STYLE = {
    "uk": "British", "united kingdom": "British", "england": "British",
    "japan": "Japanese", "france": "French", "italy": "Italian",
    "spain": "Spanish", "portugal": "Portuguese", "germany": "German",
    "australia": "Australian", "canada": "Canadian",
    "netherlands": "Dutch", "denmark": "Danish", "sweden": "Swedish",
}


def _pascal_tag(text: str) -> str:
    """名前をタグにする。'Wadhal Architects' → '#WadhalArchitects'。

    カンマ以降（', AIA' のような肩書）は落とす。記号は除き、語頭だけ
    大文字に寄せる（既に大文字が入っている語はそのまま）。
    """
    head = text.split(",")[0]
    words = re.findall(r"[A-Za-z0-9]+", head)
    if not words:
        return ""
    joined = "".join(w if w[:1].isupper() or w.isdigit() else w.capitalize() for w in words)
    return f"#{joined}" if len(joined) >= 3 else ""


def _place_tags(row: Row) -> list[str]:
    """地域のタグ。実運用の #LondonArchitecture / #CaliforniaArchitecture の形。

    州・地域（location_region）と1語の都市名から作る。国は米国以外だけ
    形容詞に変換して足す（米国は州で表す）。
    """
    tags: list[str] = []
    region = _get(row, "location_region")
    if region and len(region.split()) <= 2:
        tag = _pascal_tag(f"{region} Architecture")
        if tag:
            tags.append(tag)
    city = _get(row, "location_city")
    if city and city.isascii() and len(city.split()) == 1 and city.isalpha():
        tags.append(f"#{city.capitalize()}Architecture")
    style = _COUNTRY_STYLE.get(_get(row, "location_country").lower())
    if style:
        tags.append(f"#{style}Architecture")
    return tags


def hashtags_for(row: Row, config: CaptionConfig) -> list[str]:
    """タグの並び: ブランド → 物件名・設計者 → 様式（規則） → 地域 → 汎用。

    実運用の投稿の並びに合わせてある。重複は入れない（順序は保つ）。
    物件名タグは、名前が年号で始まるもの（'1963 Mid-Century Residence'）
    には付けない。実運用でも付けていない。
    """
    tags = list(config.hashtags)

    def add(candidates: list[str]) -> None:
        tags.extend(t for t in candidates if t and t not in tags)

    name = _get(row, "display_name")
    if name and not name[:1].isdigit():
        add([_pascal_tag(name)])
    architect = _get(row, "architect")
    if architect:
        add([_pascal_tag(architect)])

    haystack = " ".join(
        _get(row, key).lower()
        for key in ("style_name", "genre", "structure", "usage_type", "title")
    )
    for rule in config.hashtag_rules:
        if rule.match.lower() in haystack:
            add(list(rule.tags))

    add(_place_tags(row))
    add(list(config.hashtags_tail))
    return tags[:MAX_HASHTAGS]


def photo_credit(row: Row, config: CaptionConfig, source_name: str | None = None) -> str:
    """写真のクレジット1行。出せないときは空文字。

    撮影者が記事から取れていればその名前。取れなければ媒体名で代える
    （どちらも無ければ出さない）。
    """
    if not config.photo_credit_label:
        return ""
    who = _get(row, "photo_credit")
    if not who and config.photo_credit_fallback_source:
        who = (source_name or "").strip()
    return f"{config.photo_credit_label}: {who}" if who else ""


def build_alt_text(row: Row) -> str:
    """代替テキスト。読み上げと検索に効く。

    **写っているものは分からない**ので、被写体が何であるかだけを書く。
    見えていない細部を作文すると、読み上げに嘘が混ざる。
    """
    name = _get(row, "display_name") or _get(row, "title")
    parts = [p for p in (name, _place(row)) if p]
    head = ". ".join(parts)
    tail = " ".join(p for p in (_get(row, "style_name"), _get(row, "usage_type")) if p)
    text = f"{head}. {tail}".strip() if tail else head
    return text[:1000]


def _clip(text: str, limit: int = MAX_LENGTH) -> str:
    """上限で切る。切るときは末尾を落とす。"""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_caption(
    row: Row, config: CaptionConfig, source_name: str | None = None
) -> str:
    """物件1件ぶんの本文。source_name は写真のクレジットの代替に使う。"""
    blocks: list[str] = []

    if config.lead:
        blocks.append("\n".join(config.lead))

    # 見出しは短い物件名。抽出できていなければ記事の見出しで代える。
    title = _get(row, "display_name") or _get(row, "title")
    if title:
        blocks.append(f"【 {title} 】")

    specs = _spec_lines(row, config)
    if specs:
        blocks.append("\n".join(specs))

    # 説明文。日本語のみ（実運用の投稿に英語の本文は無い）。
    # 投稿用の説明文が無い古い行は、審査用の短い選定理由で代える。
    body = _get(row, "caption_body") or _get(row, "summary")
    if body:
        blocks.append(body)

    credit = photo_credit(row, config, source_name)
    if credit:
        blocks.append(credit)

    if config.details:
        blocks.append("\n".join(config.details))
    if config.signature:
        blocks.append("\n".join(config.signature))
    if config.business:
        blocks.append(config.business)
    if config.disclaimer:
        blocks.append(config.disclaimer)

    tags = hashtags_for(row, config)
    if tags:
        blocks.append(" ".join(tags))

    return _clip("\n\n".join(blocks))


def build_reel_caption(
    count: int, config: CaptionConfig, credit: str | None = None
) -> str:
    """週次リールの本文。

    物件1件の紹介ではないので仕様欄は無い。リード・締め・タグは揃える。
    credit は CC BY の音源を使ったときの表記で、**必ず入る**ように
    上限で切るのは本文側だけにしてある。
    """
    blocks: list[str] = []
    if config.lead:
        blocks.append("\n".join(config.lead))
    blocks.append(f"【 今週の{count}件 】")
    # details（ストーリーズ案内と※成約済み）は物件1件の投稿の文言なので
    # 週のまとめには入れない。
    if config.signature:
        blocks.append("\n".join(config.signature))
    tags = list(config.hashtags) + [
        t for t in config.hashtags_tail if t not in config.hashtags
    ]
    if tags:
        blocks.append(" ".join(tags[:MAX_HASHTAGS]))

    body = "\n\n".join(blocks)
    if not credit:
        return _clip(body)
    tail = f"\n\nMusic: {credit}"
    return _clip(body, MAX_LENGTH - len(tail)) + tail


def with_credit(caption: str, credit: str | None) -> str:
    """既にある本文に音源のクレジットを足す。"""
    if not credit:
        return caption
    tail = f"\n\nMusic: {credit}"
    return _clip(caption, MAX_LENGTH - len(tail)) + tail


__all__ = [
    "MAX_LENGTH",
    "build_alt_text",
    "build_caption",
    "build_reel_caption",
    "hashtags_for",
    "photo_credit",
    "with_credit",
]
