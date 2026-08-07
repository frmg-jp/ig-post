"""[9] キャプションを組む。

物件のキャプションは「どこの何か」と「なぜ選んだか」だけにする。
価格は入れない。通貨がばらばらで、為替で見え方が変わり、更新もされない。
円換算は審査UIの並べ替えにしか使わない、という判断（2026-08-07）に揃える。

リンクも入れない。Instagram のキャプション内のURLはリンクにならず、
取得元サイトの誘導にしかならないため。

**音源のクレジットは必ず最後に付ける。** CC BY の曲は表記が要件で、
落とすとライセンス違反になる。忘れないよう、キャプションを組む側の
責任にしてある（人の記憶に頼らない）。
"""

from __future__ import annotations

from freming.db.connection import Row

# Instagram のキャプションの上限。超えると投稿ごと弾かれる。
MAX_LENGTH = 2200


def _place(row: Row) -> str:
    city = (row["location_city"] or "").strip()
    country = (row["location_country"] or "").strip()
    if city and country:
        return f"{city}, {country}"
    return city or country


def _tags(hashtags: list[str]) -> str:
    cleaned = [t if t.startswith("#") else f"#{t}" for t in hashtags if t.strip()]
    return " ".join(cleaned)


def _clip(text: str, limit: int = MAX_LENGTH) -> str:
    """上限で切る。切るときは末尾を落とす（クレジットは別で足す）。"""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_caption(row: Row, hashtags: list[str] | None = None) -> str:
    """物件1件ぶんのキャプション。"""
    parts: list[str] = []
    title = (row["title"] or "").strip()
    if title:
        parts.append(title)
    place = _place(row)
    if place:
        parts.append(place)

    body = "\n".join(parts)
    summary = (row["summary"] or "").strip()
    if summary:
        body = f"{body}\n\n{summary}" if body else summary

    tags = _tags(hashtags or [])
    if tags:
        body = f"{body}\n\n{tags}" if body else tags
    return _clip(body)


def build_reel_caption(
    count: int, hashtags: list[str] | None = None, credit: str | None = None
) -> str:
    """週次リールのキャプション。

    credit は CC BY の音源を使ったときの表記。**必ず入る**ように、
    上限で切るのは本文側だけにしてある。
    """
    body = f"今週の{count}件"
    tags = _tags(hashtags or [])
    if tags:
        body = f"{body}\n\n{tags}"

    if not credit:
        return _clip(body)
    tail = f"\n\nMusic: {credit}"
    return _clip(body, MAX_LENGTH - len(tail)) + tail


def with_credit(caption: str, credit: str | None) -> str:
    """既にあるキャプションに音源のクレジットを足す。"""
    if not credit:
        return caption
    tail = f"\n\nMusic: {credit}"
    return _clip(caption, MAX_LENGTH - len(tail)) + tail


__all__ = ["MAX_LENGTH", "build_caption", "build_reel_caption", "with_credit"]
