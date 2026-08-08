"""[9] 投稿の本文を組む。

型は実際に運用している @frmg.jpn の投稿から起こした（2026-08-07）:

    リード（日英2行）
    【 物件名 】
    仕様欄（Location / Usage / Architect / …）
    説明文
    物件詳細はストーリーズ・ハイライトへ
    ※成約済みの際は…
    Design × Build × Regenerate / CURATED BY FREMING
    ＜ 海外リノベーション事業 ＞
    ハッシュタグ

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


def hashtags_for(row: Row, config: CaptionConfig) -> list[str]:
    """毎回のタグ＋物件の内容から足すタグ。

    様式・ジャンル・構造・用途をつないだ文字列に対する部分一致で決める。
    重複は入れない（順序は保つ）。
    """
    haystack = " ".join(
        _get(row, key).lower()
        for key in ("style_name", "genre", "structure", "usage_type", "title")
    )
    tags = list(config.hashtags)
    for rule in config.hashtag_rules:
        if rule.match.lower() in haystack:
            tags.extend(t for t in rule.tags if t not in tags)
    return tags


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
    parts = [p for p in (_get(row, "title"), _place(row)) if p]
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

    title = _get(row, "title")
    if title:
        blocks.append(f"【 {title} 】")

    specs = _spec_lines(row, config)
    if specs:
        blocks.append("\n".join(specs))

    # 選定理由。採点のときに書かせた一言をそのまま本文にする。
    # 日本語と英語を並べる（リード文が日英併記なので本文も揃える）。
    summary = _get(row, "summary")
    if summary:
        blocks.append(summary)
    summary_en = _get(row, "summary_en")
    if summary_en:
        blocks.append(summary_en)

    credit = photo_credit(row, config, source_name)
    if credit:
        blocks.append(credit)

    if config.details:
        blocks.append("\n".join(config.details))
    if config.disclaimer:
        blocks.append(config.disclaimer)
    if config.signature:
        blocks.append("\n".join(config.signature))
    if config.business:
        blocks.append(config.business)

    tags = hashtags_for(row, config)
    if tags:
        blocks.append("\n".join(tags))

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
    if config.details:
        blocks.append("\n".join(config.details))
    if config.signature:
        blocks.append("\n".join(config.signature))
    if config.hashtags:
        blocks.append("\n".join(config.hashtags))

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
