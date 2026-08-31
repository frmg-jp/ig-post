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


# 国名の表示。実運用の投稿は "USA" / "UK" の短い形で書いている。
_COUNTRY_DISPLAY = {
    "united states": "USA", "united states of america": "USA",
    "united kingdom": "UK",
}


def _place(row: Row) -> str:
    """Location の値。実運用の「Pasadena, California, USA」の形に合わせて
    州・地域（location_region）が取れていれば間に挟む。"""
    city = _get(row, "location_city")
    region = _get(row, "location_region")
    country = _get(row, "location_country")
    country = _COUNTRY_DISPLAY.get(country.lower(), country)
    parts = [p for p in (city, region, country) if p]
    # 都市と地域が同じ文字列のときは1つにする（"London, London" を防ぐ）
    deduped: list[str] = []
    for part in parts:
        if not deduped or deduped[-1].lower() != part.lower():
            deduped.append(part)
    return ", ".join(deduped)


# ㎡への換算。実運用の投稿は「713 sq ft (Approx. 66㎡)」の形で併記する。
_SQFT_TO_SQM = 0.09290304
_ACRE_TO_SQM = 4046.8564224


def _area_with_metric(text: str) -> str:
    """面積に㎡の併記を足す。換算できない形・既に㎡があるものはそのまま。

    抽出は原文の単位のまま（推測しない）で、換算は機械計算なのでここで行う。
    """
    lowered = text.lower()
    if "㎡" in text or "m²" in lowered or "sq m" in lowered or "sqm" in lowered:
        return text
    found = re.search(r"([\d,]+(?:\.\d+)?)", text)
    if not found:
        return text
    number = float(found.group(1).replace(",", ""))
    if "acre" in lowered:
        sqm = number * _ACRE_TO_SQM
    elif "sq" in lowered and "ft" in lowered:
        sqm = number * _SQFT_TO_SQM
    else:
        return text
    return f"{text} (Approx. {sqm:,.0f}㎡)"


def _spec_lines(row: Row, config: CaptionConfig) -> list[str]:
    """仕様欄。値のあるものだけを設定した順に並べる。"""
    lines = []
    for key, label in config.spec:
        value = _place(row) if key == "location" else _get(row, key)
        if value and key in ("building_area", "site_area"):
            value = _area_with_metric(value)
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

    # 1行目。Instagram はキャプションの1行目がユーザー名の右に食い込むので、
    # 「・」だけを置いてリード文を下に落とす（config.opener）。
    # 空行は挟まない — リードは2行目から始める。
    head = [config.opener] if config.opener else []
    head.extend(config.lead)
    if head:
        blocks.append("\n".join(head))

    # 見出しは短い物件名。抽出できていなければ記事の見出しで代える。
    title = _get(row, "display_name") or _get(row, "title")
    if title:
        blocks.append(f"【 {title} 】")

    # 写真のクレジットは仕様欄の最終行に入れる（Built in: の下）。
    # 独立した段落にしない（2026-08-22 の指示）。
    specs = _spec_lines(row, config)
    credit = photo_credit(row, config, source_name)
    if credit:
        specs.append(credit)
    if specs:
        blocks.append("\n".join(specs))

    # 説明文。日本語のみ（実運用の投稿に英語の本文は無い）。
    #
    # **summary には絶対に落とさない。** あれは審査用の選定理由で、
    # 「物語性なし」のような内部の評価がそのまま書いてある。実際に
    # 2026-08-22、caption_body の無い物件で summary が公開されてしまった。
    # 公開文に使ってよいのは、公開向けに書かせた caption_body だけ。
    body = _get(row, "caption_body")
    if body:
        blocks.append(body)

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
    count: int,
    config: CaptionConfig,
    credit: str | None = None,
    names: list[str] | None = None,
    picked_by: str = "reach",
) -> str:
    """週次リールの本文。

    物件1件の紹介ではないので仕様欄は無い。締め・タグは揃える。
    credit は CC BY の音源を使ったときの表記で、**必ず入る**ように
    上限で切るのは本文側だけにしてある。

    **入っている物件名を並べる。** 名前が無いと「今週の7件」の1行だけに
    なり、何が映るのか読む側に伝わらない。

    リードは選び方で変える。「いちばん見られた」はリーチで選べたときしか
    言えない（直近で代用したときに書くと嘘になる）。
    """
    blocks: list[str] = []
    # 通常投稿と同じ型: 1行目に「・」、リードは2行目から。
    lead = config.reel.lead_reach if picked_by == "reach" else config.reel.lead_recent
    head = [config.opener] if config.opener else []
    head.extend(lead or config.lead)
    if head:
        blocks.append("\n".join(head))

    clean = [n.strip() for n in (names or []) if n and n.strip()]
    if clean:
        if config.reel.numbered:
            # 番号は動画の並びと対応させる。「3枚目のあれ」を探せるように。
            blocks.append("\n".join(
                f"{i}  {name}" for i, name in enumerate(clean, start=1)
            ))
        else:
            blocks.append("\n".join(clean))
        if config.reel.outro:
            blocks.append("\n".join(config.reel.outro))
    else:
        # 名前が1つも取れなかったときの逃げ道。**件数だけは出す。**
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
