"""自分のアカウントの過去投稿を読む。

@frmg.jpn には、この仕組みを通す前に**手で運用していた投稿**がある。
週次リールの材料が足りないとき、そこから絵を持ってこられるようにする。

## 自分のアカウントだけ

読むのは `/me/media`、つまり**自分が出した投稿**だけ。他人のアカウントを
読む経路はここに作らない（規約と、写真の権利の両方が理由。詳細は
`docs/europe-cheap-houses.md`）。自分が既に公開した画像を自分のリールに
入れ直すのは、元の投稿と同じ立場のまま。

## カルーセルは1枚目

投稿は納品と同じ並びのカルーセルで出している。リールに使うのは各投稿の
表紙にあたる1枚目。`children` の先頭がそれに当たる。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from freming.instagram.publish import API_VERSION, GRAPH, InstagramError, _request
from freming.logging_setup import get_logger

log = get_logger(__name__)

_FIELDS = (
    "id,media_type,media_url,thumbnail_url,permalink,timestamp,caption,"
    "children{media_url,media_type}"
)


@dataclass
class MediaItem:
    """自分の投稿1件。"""

    id: str
    media_type: str
    permalink: str
    timestamp: str
    caption: str
    image_url: str | None      # リールに使う1枚（カルーセルなら先頭）
    child_count: int = 0

    def head(self, width: int = 44) -> str:
        """一覧に出す1行分の見出し。**本文の1行目は「・」なので飛ばす。**"""
        for line in (self.caption or "").splitlines():
            text = line.strip()
            if text and text != "・":
                return text[:width]
        return "（本文なし）"


def _cover(row: dict) -> tuple[str | None, int]:
    """その投稿の表紙になる画像のURLと、カルーセルの枚数。"""
    children = (row.get("children") or {}).get("data") or []
    if children:
        first = children[0]
        return first.get("media_url"), len(children)
    if row.get("media_type") == "VIDEO":
        # 動画の表紙。リールを材料にすることは想定していないが、
        # 一覧に出したときに欄が空になるのを避ける。
        return row.get("thumbnail_url"), 0
    return row.get("media_url"), 0


def recent_media(token: str, ig_id: str, limit: int = 25) -> list[MediaItem]:
    """自分の直近の投稿を新しい順に返す。"""
    body = _request(
        "GET", f"{GRAPH}/{API_VERSION}/{ig_id}/media", token,
        params={"fields": _FIELDS, "limit": limit},
    )
    items = []
    for row in body.get("data", []):
        url, count = _cover(row)
        items.append(
            MediaItem(
                id=str(row.get("id", "")),
                media_type=row.get("media_type", ""),
                permalink=row.get("permalink", ""),
                timestamp=row.get("timestamp", ""),
                caption=row.get("caption", "") or "",
                image_url=url,
                child_count=count,
            )
        )
    return items


def get_media(token: str, media_id: str) -> MediaItem:
    """投稿を1件だけ読む。"""
    row = _request(
        "GET", f"{GRAPH}/{API_VERSION}/{media_id}", token, params={"fields": _FIELDS}
    )
    url, count = _cover(row)
    return MediaItem(
        id=str(row.get("id", media_id)),
        media_type=row.get("media_type", ""),
        permalink=row.get("permalink", ""),
        timestamp=row.get("timestamp", ""),
        caption=row.get("caption", "") or "",
        image_url=url,
        child_count=count,
    )


def download_image(url: str, dest: Path) -> Path:
    """画像を保存する。**中身が画像であることを確かめてから書く。**

    CDNのURLは期限付きで、切れるとHTMLのエラーページが返る。拡張子だけ
    見て保存すると、リールを組む段になって「壊れたJPEG」で落ちる。
    """
    import httpx

    response = httpx.get(url, timeout=60, follow_redirects=True)
    response.raise_for_status()
    kind = response.headers.get("content-type", "")
    if not kind.startswith("image/"):
        raise InstagramError(
            f"画像が返ってきませんでした（content-type: {kind or '不明'}）。"
            "メディアURLは期限付きなので、取り直してください。"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    log.info("保存しました: %s（%d KB）", dest, len(response.content) // 1024)
    return dest


__all__ = ["MediaItem", "download_image", "get_media", "recent_media"]
