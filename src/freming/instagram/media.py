"""[9] 投稿する画像を「Meta が取りに来られる場所」に置く。

Meta は投稿のたびにこちらのサーバーへ画像を取りに来る（公式ドキュメントの
"We cURL media used in publishing attempts"）。つまり **公開URLが要る**。
Drive のリンクは使えない。

置き場をDBにしている理由:

  - 審査UI（Render）のディスクは揮発する。再起動で消えたら投稿が失敗する
  - 納品済みの加工画像は**納品ワーカーが動いた Mac にしかない**。
    投稿は Render 側で動くので、ファイルは共有されていない
  - DBなら審査UIも定期実行も同じものを見られる。既にそうなっている

元画像の探し方は2段構え。手元にあるならファイルを読み、無ければ
取得元から取り直して同じ加工をかける。どちらの環境でも同じ絵になる。

投稿が済んだ行は消す。溜めても使い道がなく、容量を食うだけ。
"""

from __future__ import annotations

import secrets
from io import BytesIO
from pathlib import Path

from freming.config import Config
from freming.db.connection import DbConnection
from freming.logging_setup import get_logger

log = get_logger(__name__)

TOKEN_BYTES = 24


class MediaError(RuntimeError):
    """投稿する画像を用意できなかった。"""


def new_token() -> str:
    """推測できないURL用の文字列。連番は使わない。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def _square_from_disk(conn: DbConnection, property_id: int, position: int) -> bytes | None:
    row = conn.execute(
        "SELECT output_path FROM images WHERE property_id = ? AND position = ?",
        (property_id, position),
    ).fetchone()
    if row is None or not row["output_path"]:
        return None
    path = Path(row["output_path"])
    return path.read_bytes() if path.exists() else None


def _square_from_source(
    config: Config, conn: DbConnection, property_id: int, position: int
) -> bytes | None:
    """取得元から取り直して、納品と同じ 1080×1080 に加工する。

    納品ワーカーが動いた環境以外（Render）ではこちらを通る。相手サイトへの
    アクセスなので、収集と同じ HttpClient を使う（robots.txt の確認と
    ドメイン間隔がそのまま効く）。
    """
    from freming.images.process import to_square
    from freming.net.client import HttpClient

    row = conn.execute(
        "SELECT source_url FROM images WHERE property_id = ? AND position = ?",
        (property_id, position),
    ).fetchone()
    if row is None:
        return None

    import tempfile

    with HttpClient(config.http) as http:
        try:
            response = http.get(row["source_url"])
        except Exception as exc:  # noqa: BLE001 - 取得失敗は上で MediaError にする
            log.warning("画像を取り直せませんでした: %s（%s）", row["source_url"], exc)
            return None
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dst = Path(tmp) / "out.jpg"
            src.write_bytes(response.content)
            to_square(src, dst, config.process)
            return dst.read_bytes()


def square_bytes(
    config: Config, conn: DbConnection, property_id: int, position: int = 1
) -> bytes:
    """物件の N 枚目を 1080×1080 の JPEG として返す。"""
    data = _square_from_disk(conn, property_id, position)
    if data is None:
        data = _square_from_source(config, conn, property_id, position)
    if not data:
        raise MediaError(
            f"property_id={property_id} の {position} 枚目を用意できませんでした。"
            "画像が未取得か、取得元から消えている可能性があります。"
        )
    return data


def to_vertical(square: bytes, config: Config) -> bytes:
    """正方形をストーリーズ用の 1080×1920 にする。

    リールと同じ組み方（同じ写真をぼかして背景に敷く）。ストーリーズだけ
    別の見え方にする理由がないので、`reel.build.compose_frame` を使い回す。
    """
    import tempfile

    from freming.reel.build import compose_frame

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "square.jpg"
        dst = Path(tmp) / "vertical.jpg"
        src.write_bytes(square)
        compose_frame(src, dst, config.reel)
        return dst.read_bytes()


# ----------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------
def store_media(
    conn: DbConnection,
    post_id: int,
    content: bytes,
    mime: str = "image/jpeg",
    position: int = 1,
    now: str | None = None,
) -> str:
    """投稿1件ぶんの実体を置き、配るための token を返す。"""
    from datetime import UTC, datetime

    token = new_token()
    conn.execute(
        "INSERT INTO post_media (post_id, token, position, mime, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (post_id, token, position, mime, content, now or datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return token


def load_media(conn: DbConnection, token: str) -> tuple[bytes, str] | None:
    """token から実体を引く。審査UIの /m/<token> が使う。"""
    row = conn.execute(
        "SELECT content, mime FROM post_media WHERE token = ?", (token,)
    ).fetchone()
    if row is None:
        return None
    content = row["content"]
    # psycopg は bytea を memoryview で返す。そのまま返すと
    # Content-Length の計算やテストの比較で扱いにくい。
    return (bytes(content), row["mime"])


def media_tokens(conn: DbConnection, post_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT token FROM post_media WHERE post_id = ? ORDER BY position, id",
        (post_id,),
    ).fetchall()
    return [row["token"] for row in rows]


def purge_media(conn: DbConnection, post_id: int) -> int:
    """投稿が済んだ実体を消す。戻り値は消した枚数。"""
    cursor = conn.execute("DELETE FROM post_media WHERE post_id = ?", (post_id,))
    conn.commit()
    return cursor.rowcount or 0


def probe_size(data: bytes) -> tuple[int, int] | None:
    """寸法を返す。投稿前の確認用。"""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as img:
            return img.size
    except (UnidentifiedImageError, OSError):
        return None


__all__ = [
    "MediaError",
    "load_media",
    "media_tokens",
    "new_token",
    "probe_size",
    "purge_media",
    "square_bytes",
    "store_media",
    "to_vertical",
]
