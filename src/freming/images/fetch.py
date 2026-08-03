"""[4] 承認済み物件の画像を取得する。

    記事ページを1回取得 → 画像URLを抽出 → 上から順にダウンロード

記事ページの取得は物件あたり1回。robots.txt とリクエスト間隔は
HttpClient が担保するので、ここでは HTTP を直接触らない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from freming.config import Config
from freming.db.connection import DbConnection, Row
from freming.images.extract import extract_image_urls
from freming.logging_setup import get_logger
from freming.net.client import HttpClient, RobotsDisallowed

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class NoImagesFound(RuntimeError):
    """使える画像が1枚も取れなかった。納品はできない。"""


@dataclass
class FetchedImage:
    source_url: str
    local_path: Path
    width: int
    height: int
    position: int


@dataclass
class FetchStats:
    property_id: int
    found_urls: int = 0
    downloaded: int = 0
    too_small: int = 0
    wrong_type: int = 0
    failed: int = 0
    # 前回までに取得済み・除外済みのURL。再実行時はここに入るので
    # ダウンロードは発生しない（相手サイトへのリクエストが増えない）。
    already_have: int = 0
    skipped_before: int = 0
    images: list[FetchedImage] = field(default_factory=list)

    @property
    def usable(self) -> int:
        """加工に回せる枚数。今回取得した分と、前回までの取得済みの合計。"""
        return self.downloaded + self.already_have

    def summary(self) -> str:
        # 「採用 0 枚」だけを出すと、再実行時に取得済み10枚があっても
        # 失敗したように読める。手元の枚数と、今回の増分を分けて出す。
        head = (
            f"[画像] property_id={self.property_id} "
            f"候補URL {self.found_urls} → 手元 {self.usable} 枚"
            f"（新規 {self.downloaded} / 取得済み {self.already_have}）"
        )
        dropped = (
            f"小さすぎ {self.too_small} / 形式外 {self.wrong_type} / "
            f"失敗 {self.failed} / 除外済み {self.skipped_before}"
        )
        return f"{head}（{dropped}）"


_SAFE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def _suffix_of(url: str) -> str:
    """保存するファイルの拡張子。クエリ文字列を含めない。

    CDN配信のURLは `photo.jpg?w=2000&format=webp` のようにクエリが付く。
    URL全体から拡張子を取ると `.jpg?w=2000&format=webp` がそのまま
    ファイル名になり、扱いにくいうえ環境によっては保存できない。
    """
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in _SAFE_SUFFIXES else ".jpg"


def _probe(data: bytes) -> tuple[int, int] | None:
    """バイト列から画像サイズを読む。壊れていれば None。"""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as img:
            return img.size
    except (UnidentifiedImageError, OSError):
        return None


def fetch_images(
    config: Config,
    conn: DbConnection,
    row: Row,
    client: HttpClient | None = None,
) -> FetchStats:
    """1物件分の画像を取得してディスクとDBに記録する。

    既に取得済みの画像は再ダウンロードしない（images テーブルの
    (property_id, source_url) が UNIQUE）。再実行しても相手サイトへの
    リクエストが増えないようにする。
    """
    stats = FetchStats(property_id=int(row["id"]))
    work_dir = Path(config.images.work_dir) / f"p{row['id']:06d}"
    work_dir.mkdir(parents=True, exist_ok=True)

    owns_client = client is None
    client = client or HttpClient(config.http)
    try:
        try:
            article = client.get(row["source_url"])
        except RobotsDisallowed:
            log.error(
                "robots.txt が画像取得を許可していません: %s\n"
                "  User-Agent の偽装による回避は行いません。手動で画像を用意してください。",
                row["source_url"],
            )
            raise NoImagesFound(f"robots.txt により取得できません: {row['source_url']}") from None

        # 代表画像が合成のメディアでは先頭を飛ばす（01.jpg が顔写真入りの
        # 合成画像になるのを避ける）。設定はソース側に持たせてある。
        source = config.editorial_source(row["source"])
        urls = extract_image_urls(
            article.text,
            row["source_url"],
            skip_lead_image=bool(source and source.skip_lead_image),
        )
        stats.found_urls = len(urls)
        log.info("画像URLを %d 件見つけました: property_id=%s", len(urls), row["id"])

        existing = {
            r["source_url"]
            for r in conn.execute(
                "SELECT source_url FROM images WHERE property_id = ?", (row["id"],)
            )
        }
        # 一度採用しなかったURLは二度と取りに行かない。判定基準（最小サイズ、
        # 許可する形式）は設定で変わりうるが、変えたときは image_skips を
        # 消せば再取得できる。既定では無駄なリクエストを繰り返さない方を採る。
        skipped = {
            r["source_url"]
            for r in conn.execute(
                "SELECT source_url FROM image_skips WHERE property_id = ?", (row["id"],)
            )
        }

        def _skip(url: str, reason: str) -> None:
            conn.execute(
                "INSERT INTO image_skips (property_id, source_url, reason, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (property_id, source_url) DO NOTHING",
                (row["id"], url, reason, _now()),
            )
            conn.commit()

        position = conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS p FROM images WHERE property_id = ?",
            (row["id"],),
        ).fetchone()["p"]

        for url in urls:
            if stats.downloaded + len(existing) >= config.images.max_per_property:
                break
            if url in existing:
                stats.already_have += 1
                continue
            if url in skipped:
                stats.skipped_before += 1
                continue
            try:
                response = client.get(url)
            except RobotsDisallowed:
                log.info("robots.txt により取得しません: %s", url)
                _skip(url, "robots")
                stats.failed += 1
                continue
            except Exception as exc:  # noqa: BLE001 - 1枚の失敗で残りを止めない
                log.warning("画像を取得できませんでした: %s (%s)", url, exc)
                _skip(url, "failed")
                stats.failed += 1
                continue

            content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
            if content_type not in config.images.allowed_content_types:
                _skip(url, "wrong_type")
                stats.wrong_type += 1
                continue

            size = _probe(response.content)
            if size is None:
                _skip(url, "broken")
                stats.failed += 1
                continue
            width, height = size
            if min(width, height) < config.images.min_short_edge_px:
                # ロゴ・アイコン・サムネイル版はここで落ちる
                _skip(url, "too_small")
                stats.too_small += 1
                continue

            position += 1
            path = work_dir / f"{position:02d}{_suffix_of(url)}"
            path.write_bytes(response.content)

            conn.execute(
                "INSERT INTO images "
                "(property_id, source_url, width, height, local_path, position, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (property_id, source_url) DO NOTHING",
                (row["id"], url, width, height, str(path), position, _now()),
            )
            conn.commit()
            stats.downloaded += 1
            stats.images.append(
                FetchedImage(url, path, width, height, position)
            )
    finally:
        if owns_client:
            client.close()

    log.info(stats.summary())
    if stats.downloaded == 0 and not existing:
        raise NoImagesFound(
            f"使える画像が見つかりませんでした（候補URL {stats.found_urls} 件、"
            f"短辺 {config.images.min_short_edge_px}px 未満 {stats.too_small} 件）"
        )
    return stats
