"""[9] 予定を見て、時間が来たものを投稿する。

審査UIと同じプロセスで動かす（納品ワーカーと同じ形）。別プロセスに
しないのは、**投稿する画像を配るのが審査UI自身だから**。Meta は
`instagram.public_base_url` に取りに来るので、配る側と投稿する側が
同じ場所にいるほうが状態がずれない。

守っていること:

  - **1件ずつ直列。** まとめて投げない。Meta 側で失敗したときに
    どこまで進んだか分かるようにする
  - 予定の取得と `publishing` への変更を1文でやる（repository.claim_due_post）。
    2箇所でワーカーが動いても同じ行を取れない
  - 失敗は attempts に数え、上限で止める。取れない投稿を延々と
    投げ続けない。復帰は審査UIの「予定に戻す」から
  - **リーチが取れないときリールを作らない。** 代わりの7枚で黙って
    出すと、狙いと違うものが出たことに誰も気づけない
    （instagram.reel_fallback_recent を true にしたときだけ代用する）
"""

from __future__ import annotations

import tempfile
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from freming.config import Config
from freming.db.connection import DbConnection, Row, connect
from freming.db.repository import (
    abandon_post,
    claim_due_post,
    fail_post,
    finish_post,
    published_posts_between,
    record_reach,
    set_permalink,
)
from freming.instagram import media
from freming.instagram.caption import build_alt_text, build_reel_caption
from freming.instagram.insights import MissingInsightsScope, media_reach
from freming.instagram.publish import (
    KIND_FEED,
    KIND_REEL,
    KIND_STORY,
    account_id,
    media_permalink,
    publish_image,
    publish_reel,
)
from freming.instagram.tokens import InstagramError, load_token
from freming.logging_setup import get_logger

log = get_logger(__name__)


class PostingError(RuntimeError):
    """投稿を1件やり切れなかった。理由は必ずメッセージに入れる。"""


def describe_error(exc: BaseException) -> str:
    head = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {head[0]}" if head else type(exc).__name__


# ----------------------------------------------------------------------
# 1件を投稿する
# ----------------------------------------------------------------------
def _publish_feed(config: Config, conn: DbConnection, post: Row, token: str, ig_id: str):
    square = media.square_bytes(config, conn, post["property_id"], position=1)
    media_token = media.store_media(conn, post["id"], square)
    url = config.instagram.public_media_url(media_token)
    row = conn.execute(
        "SELECT * FROM properties WHERE id = ?", (post["property_id"],)
    ).fetchone()
    alt = build_alt_text(row) if row is not None else None
    return publish_image(token, ig_id, url, post["caption"], alt_text=alt)


def _publish_story(config: Config, conn: DbConnection, post: Row, token: str, ig_id: str):
    square = media.square_bytes(config, conn, post["property_id"], position=1)
    vertical = media.to_vertical(square, config)
    media_token = media.store_media(conn, post["id"], vertical)
    url = config.instagram.public_media_url(media_token)
    return publish_image(token, ig_id, url, None, story=True)


def daily_winners(
    config: Config, conn: DbConnection, token: str, now: datetime
) -> list[Row]:
    """直近7日の各日で、いちばんリーチした投稿を集める。

    日をまたいで比べないのは、リーチが時間とともに伸びるため。
    同じ日の3本同士なら成熟度が揃う。
    """
    zone = ZoneInfo(config.instagram.timezone)
    since = now - timedelta(days=8)
    rows = published_posts_between(conn, since.isoformat(), now.isoformat())

    by_day: dict[str, list[tuple[int, Row]]] = defaultdict(list)
    for row in rows:
        published = datetime.fromisoformat(row["published_at"]).astimezone(zone)
        reach = row["reach"]
        if reach is None and row["ig_media_id"]:
            reach = media_reach(token, row["ig_media_id"])
            record_reach(conn, row["id"], reach)
        by_day[published.date().isoformat()].append((reach or 0, row))

    winners = []
    for day in sorted(by_day)[-7:]:
        best = max(by_day[day], key=lambda pair: pair[0])
        winners.append(best[1])
    return winners


def _publish_reel(config: Config, conn: DbConnection, post: Row, token: str, ig_id: str):
    from freming.reel.build import audio_for_week, build_reel

    now = datetime.now(UTC)
    try:
        winners = daily_winners(config, conn, token, now)
    except MissingInsightsScope:
        if not config.instagram.reel_fallback_recent:
            raise
        log.warning("リーチが読めないので、直近の投稿で代用します")
        since = (now - timedelta(days=8)).isoformat()
        winners = list(published_posts_between(conn, since, now.isoformat()))[-7:]

    if len(winners) < 2:
        raise PostingError(
            f"リールに使える投稿が {len(winners)} 件しかありません。"
            "先週の投稿が揃ってから作り直してください。"
        )

    track = audio_for_week(int(now.strftime("%V")))
    caption = build_reel_caption(len(winners), config.caption, track.caption_line())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        squares = []
        for index, row in enumerate(winners, start=1):
            path = work / f"{index:02d}.jpg"
            path.write_bytes(media.square_bytes(config, conn, row["property_id"], 1))
            squares.append(path)
        video = work / "reel.mp4"
        build_reel(squares, track, video, config.reel, work_dir=work / "frames")
        conn.execute(
            "UPDATE posts SET caption = ?, credit = ? WHERE id = ?",
            (caption, track.caption_line() or None, post["id"]),
        )
        conn.commit()
        return publish_reel(token, ig_id, video, caption)


_HANDLERS = {
    KIND_FEED: _publish_feed,
    KIND_STORY: _publish_story,
    KIND_REEL: _publish_reel,
}


def publish_one(config: Config, conn: DbConnection, post: Row, token: str, ig_id: str) -> str:
    """予定1件を投稿する。戻り値は media_id。"""
    handler = _HANDLERS.get(post["kind"])
    if handler is None:
        raise PostingError(f"知らない種別です: {post['kind']}")

    # ストーリーズは元の投稿が出ていることが前提。順序が入れ替わると
    # 「まだ出ていない投稿のストーリーズ」になる。
    if post["kind"] == KIND_STORY and post["parent_post_id"]:
        parent = conn.execute(
            "SELECT state FROM posts WHERE id = ?", (post["parent_post_id"],)
        ).fetchone()
        if parent is not None and parent["state"] != "published":
            raise PostingError("元の投稿がまだ公開されていません。次回に回します。")

    result = handler(config, conn, post, token, ig_id)
    finish_post(conn, post["id"], result.media_id, result.container_id)
    if post["kind"] == KIND_FEED:
        # ストーリーズは手で追加する（API がリンク付きの再共有を開けていない）。
        # 人が「投稿を開く」ためのURLをここで取っておく。取れなくても
        # 投稿自体は成立しているので、失敗しても止めない。
        set_permalink(conn, post["id"], media_permalink(token, result.media_id))
    removed = media.purge_media(conn, post["id"])
    if removed:
        log.info("配り終えた画像を %d 件消しました（post_id=%s）", removed, post["id"])
    return result.media_id


def run_once(config: Config, conn: DbConnection, now: datetime | None = None) -> int:
    """時間が来た予定を順に投稿する。戻り値は投稿できた件数。"""
    now = now or datetime.now(UTC)
    record = load_token(conn)
    if record is None:
        log.info("Instagram のトークンが未設定のため、投稿は行いません")
        return 0
    if not config.instagram.public_base_url:
        log.warning(
            "instagram.public_base_url が未設定です。"
            "Meta が画像を取りに来られないため、投稿は行いません"
        )
        return 0

    ig_id = account_id(record.value)
    done = 0
    while True:
        post = claim_due_post(conn, now.isoformat(), config.instagram.max_attempts)
        if post is None:
            return done
        # 設定を切り替える前に作られたストーリーズの予定が残っていることが
        # ある。**止めたつもりのものが出る**のが一番まずいので、ここで落とす。
        if post["kind"] == KIND_STORY and not config.instagram.post_story:
            abandon_post(conn, post["id"])
            log.info("自動ストーリーズは無効なので見送りました（post_id=%s）", post["id"])
            continue
        try:
            publish_one(config, conn, post, record.value, ig_id)
            done += 1
        except (InstagramError, PostingError, media.MediaError, OSError) as exc:
            state = fail_post(
                conn, post["id"], describe_error(exc), config.instagram.max_attempts
            )
            log.warning(
                "投稿に失敗しました（post_id=%s / %s）: %s",
                post["id"], "打ち切り" if state == "failed" else "次回に再試行", exc,
            )


class PostingWorker:
    """予定を定期的に見て投稿するスレッド。納品ワーカーと同じ作り。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="freming-posting", daemon=True)
        self._thread.start()
        log.info(
            "自動投稿を開始しました（巡回間隔 %.0f 秒）",
            self.config.instagram.poll_interval_sec,
        )

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stopping.is_set():
            conn = None
            try:
                conn = connect(self.config.app.target())
                run_once(self.config, conn)
                self.last_error = None
            except Exception as exc:  # ワーカーは1回の失敗で死なせない
                self.last_error = describe_error(exc)
                log.exception("投稿ワーカーで例外が出ました")
            finally:
                if conn is not None:
                    conn.close()
            self._stopping.wait(self.config.instagram.poll_interval_sec)


__all__ = [
    "PostingError",
    "PostingWorker",
    "daily_winners",
    "describe_error",
    "publish_one",
    "run_once",
]
