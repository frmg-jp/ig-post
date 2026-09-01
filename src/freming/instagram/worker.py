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
  - **週次リールの材料はアカウントの実物**（/me/media）。予定表からは
    組まない。予定表だけを見ると手で出した投稿が抜け、写真も投稿の表紙と
    違うカットになる（2026-09-01 に両方起きた）
  - リーチが読めないときは「先に出た方」で代用する
    （instagram.reel_fallback_recent を true にしたときだけ）
"""

from __future__ import annotations

import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
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
    publish_carousel,
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
def _parse_image_order(text: str | None) -> list[int]:
    """「3,1,2」を [3, 1, 2] に。壊れた値は無視して既定の並びに落とす。"""
    if not text:
        return []
    out: list[int] = []
    for piece in str(text).split(","):
        piece = piece.strip()
        if not piece.isdigit():
            return []
        value = int(piece)
        if value not in out:
            out.append(value)
    return out


def _publish_feed(config: Config, conn: DbConnection, post: Row, token: str, ig_id: str):
    """通常投稿。**納品と同じ並びの複数枚をカルーセルで出す。**

    1枚目は必須（用意できなければ投稿ごと失敗させる）。2枚目以降は
    取得元から消えていることがあるので、欠けた分は飛ばして詰める。
    結果的に1枚しか無ければ、1枚の通常投稿として出す。
    """
    property_id = post["property_id"]
    row = conn.execute(
        "SELECT * FROM properties WHERE id = ?", (property_id,)
    ).fetchone()

    # 予定は作られたが材料が無い、という行が残っていることがある
    # （postable_properties の絞り込みより前に作られた予定）。
    # そのまま出すと見出しが住所・本文が審査用の文章になるので、出さない。
    #
    # **人が本文を書いた場合（caption_edited_at 付き）は例外。** 公開文を
    # 人が用意したなら、抽出できなかったことは問題にならない。
    if row is None:
        raise PostingError("物件が見つかりません。")
    edited = bool(post["caption_edited_at"]) if "caption_edited_at" in post.keys() else False
    if not edited and not (row["display_name"] and row["caption_body"]):
        raise PostingError(
            "投稿の材料（物件名・説明文）が揃っていません。"
            "審査UIで本文を手で書けば出せます。出さないなら見送ってください。"
        )
    alt = build_alt_text(row)

    positions = media.available_positions(conn, property_id)
    # 審査UIで並びを直してあれば、その順で出す。並びに無い番号は無視し、
    # 並びから漏れた番号は後ろに足す（写真を黙って落とさない）。
    custom = _parse_image_order(post["image_order"] if "image_order" in post.keys() else None)
    if custom:
        ordered = [p for p in custom if p in positions]
        ordered += [p for p in positions if p not in ordered]
        positions = ordered
    positions = positions[: config.instagram.carousel_max]

    urls: list[str] = []
    for index, position in enumerate(positions, start=1):
        try:
            square = media.square_bytes(config, conn, property_id, position=position)
        except media.MediaError:
            if position == positions[0]:
                raise  # 1枚目が無いなら出せない。理由ごと上へ
            log.warning(
                "%d 枚目を用意できなかったので飛ばします（property_id=%s）",
                position, property_id,
            )
            continue
        media_token = media.store_media(conn, post["id"], square, position=index)
        urls.append(config.instagram.public_media_url(media_token))

    if len(urls) == 1:
        return publish_image(token, ig_id, urls[0], post["caption"], alt_text=alt)
    return publish_carousel(token, ig_id, urls, post["caption"], alt_text=alt)


def _publish_story(config: Config, conn: DbConnection, post: Row, token: str, ig_id: str):
    square = media.square_bytes(config, conn, post["property_id"], position=1)
    vertical = media.to_vertical(square, config)
    media_token = media.store_media(conn, post["id"], vertical)
    url = config.instagram.public_media_url(media_token)
    return publish_image(token, ig_id, url, None, story=True)


def last_week(config: Config, now: datetime) -> tuple[datetime, datetime]:
    """先週の月曜0時から、今週の月曜0時まで。現地時刻で切って UTC で返す。

    **移動窓（now から8日）にしない。** GitHub の定期実行は3〜10時間ずれる
    ので、移動窓だと同じ月曜の枠でも走った時刻で入る日が変わる。月曜19:00
    に走っても、遅れて火曜05:00 に走っても、同じ「先週」を指すようにする。

    月曜に出すリールが指すのは、前日の日曜で終わった週。火曜にずれ込んでも
    同じ週を指す（weekday() は月曜が0なので、月曜も火曜も同じ「今週の月曜」
    を基点に持つ）。
    """
    zone = ZoneInfo(config.instagram.timezone)
    local = now.astimezone(zone)
    monday = datetime.combine(
        local.date() - timedelta(days=local.weekday()), time.min, tzinfo=zone
    )
    return (monday - timedelta(days=7)).astimezone(UTC), monday.astimezone(UTC)


@dataclass
class WeekPick:
    """リールに入れる1本。**アカウントに実際に出ているもの。**"""

    media_id: str
    published: datetime      # 現地時刻
    image_url: str
    name: str                # 本文に並べる見出し
    reach: int | None = None


def weekly_picks(
    config: Config, conn: DbConnection, token: str, ig_id: str, now: datetime,
    *, use_reach: bool = True,
) -> tuple[list[WeekPick], str]:
    """先週アカウントに出た投稿から、1日1本ずつ選ぶ。

    **予定表（posts）ではなくアカウントの実物を見る。** 予定表だけを見ると、
    手で出した投稿が丸ごと抜ける。2026-09-01 の初回がそれで、08/27 に手で
    出した1本を落とし、7軒あるところを6軒として出した。

    見出しは予定表から引く（ig_media_id が一致する行の物件名）。手で出した
    投稿は予定表に無いので、本文の【 】から読む（_pick_name）。

    戻り値は (選んだ順, 選び方)。選び方は reach か recent。
    """
    from freming.instagram.mymedia import recent_media

    zone = ZoneInfo(config.instagram.timezone)
    start, end = last_week(config, now)

    by_day: dict[object, list[WeekPick]] = defaultdict(list)
    for item in recent_media(token, ig_id, limit=50):
        if not item.timestamp or not item.image_url:
            continue
        # **動画は入れない。** リール自身が翌週の材料になってしまう。
        if item.media_type in ("VIDEO", "REELS"):
            continue
        moment = datetime.fromisoformat(item.timestamp.replace("+0000", "+00:00"))
        if not (start <= moment < end):
            continue
        local = moment.astimezone(zone)
        by_day[local.date()].append(
            WeekPick(item.id, local, item.image_url, _pick_name(config, conn, item))
        )

    picked_by = "recent"
    if use_reach:
        try:
            for picks in by_day.values():
                for pick in picks:
                    pick.reach = media_reach(token, pick.media_id)
            picked_by = "reach"
        except MissingInsightsScope:
            if not (
                config.instagram.reel_fallback_recent if use_reach else True
            ):
                raise
            log.warning("リーチが読めないので、先に出た方を採ります")
            picked_by = "recent"

    out: list[WeekPick] = []
    for day in sorted(by_day):
        same_day = by_day[day]
        if picked_by == "reach":
            out.append(max(same_day, key=lambda p: p.reach or 0))
        else:
            # どちらが良かったかは分からない。**先に出た方**を採る。
            out.append(min(same_day, key=lambda p: p.published))

    # **選抜が起きていない週は「いちばん見られた」と名乗らない。**
    # 1日1本しか出していなければ、日ごとの1位＝その日の唯一の1本。
    if picked_by == "reach" and all(len(v) == 1 for v in by_day.values()):
        picked_by = "recent"
    return out, picked_by


def _pick_name(config: Config, conn: DbConnection, item) -> str:
    """本文に並べる見出し。予定表にあれば物件名、無ければ本文の1行目。"""
    row = conn.execute(
        "SELECT p.display_name, p.title FROM posts AS o "
        "JOIN properties AS p ON p.id = o.property_id "
        "WHERE o.ig_media_id = ?",
        (item.id,),
    ).fetchone()
    if row is not None:
        name = row["display_name"] or row["title"] or ""
        if name.strip():
            return name.strip()
    # 予定表に無い投稿（手で出したもの）。**本文から名前を読む。**
    #
    # 通常投稿の本文はこの型で出している:
    #
    #     ・
    #     世界で今、手に入る"気になる建築・不動産"を…   ← config.lead（定型）
    #
    #     【 The Eyebrow House 】                      ← 名前はここ
    #
    # item.head() は「1行目の『・』を飛ばして2行目」なので、**定型のリード
    # 文を拾ってしまう**。2026-09-01 の試写で、手で出した1本だけ見出しが
    # 「世界で今、手に入る…」になった。名前は本文の中にある。
    import re

    found = re.search(r"【\s*(.+?)\s*】", item.caption or "")
    if found:
        return found.group(1).strip()

    # 型に沿っていない本文への逃げ道。定型のリード文は名前ではないので飛ばす。
    skip = {config.caption.opener, *config.caption.lead}
    for line in (item.caption or "").splitlines():
        text = line.strip()
        if text and text not in skip:
            return text[:80]
    return ""


@dataclass
class WeeklyReel:
    """組み上がった週次リール1本。"""

    video: Path
    winners: list["WeekPick"]
    track: object            # reel.build.Track
    caption: str
    result: object           # reel.build.ReelResult
    picked_by: str = "reach"  # reach = 日ごとのリーチ1位 / recent = 直近で代用


def build_weekly_reel(
    config: Config,
    conn: DbConnection,
    token: str,
    video: Path,
    now: datetime | None = None,
    work_dir: Path | None = None,
    allow_fallback: bool | None = None,
    ig_id: str | None = None,
) -> WeeklyReel:
    """週次リールを1本組む。**投稿はしない。**

    出す処理と試写の両方がここを通る。**別々に書くと、試写で見たものと
    出るものが食い違う。** 食い違う試写は無いより悪い。

    材料は**アカウントに実際に出ている投稿**（weekly_picks）。予定表から
    組むのはやめた。予定表だけを見ると、手で出した投稿が丸ごと抜けるうえ、
    写真も「物件の1枚目」になり、審査UIで並べ替えた投稿とは別のカットに
    なる。2026-09-01 の初回で両方が起きた（7軒あるところを6軒で出し、
    表紙も投稿と違うものが並んだ）。

    allow_fallback は「リーチが読めないとき先に出た方で代用してよいか」。
    既定（None）は config に従う。**どちらで選んだかは picked_by に残す。**
    """
    from freming.images.process import to_square
    from freming.instagram.mymedia import download_image
    from freming.reel.build import audio_for_week, build_reel

    now = now or datetime.now(UTC)
    if allow_fallback is None:
        allow_fallback = config.instagram.reel_fallback_recent
    if ig_id is None:
        ig_id = account_id(token)

    try:
        picks, picked_by = weekly_picks(config, conn, token, ig_id, now)
    except MissingInsightsScope:
        if not allow_fallback:
            raise
        log.warning("リーチが読めないので、先に出た方を採ります")
        picks, picked_by = weekly_picks(
            config, conn, token, ig_id, now, use_reach=False
        )

    if len(picks) < 2:
        raise PostingError(
            f"リールに使える投稿が {len(picks)} 件しかありません。"
            "先週（月〜日）にアカウントへ出た投稿から選びます。"
        )

    track = audio_for_week(int(now.strftime("%V")))
    caption = build_reel_caption(
        len(picks), config.caption, track.caption_line(),
        names=[pick.name for pick in picks], picked_by=picked_by,
    )

    video.parent.mkdir(parents=True, exist_ok=True)
    frames = work_dir or video.parent / f".{video.stem}-frames"
    squares = []
    for index, pick in enumerate(picks, start=1):
        raw = frames / f"raw-{index:02d}.jpg"
        path = frames / f"src-{index:02d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        download_image(pick.image_url, raw)
        # 手で出した投稿は正方形とは限らない。**必ず揃える。**
        to_square(raw, path, config.process)
        squares.append(path)
    result = build_reel(squares, track, video, config.reel, work_dir=frames)
    return WeeklyReel(video, picks, track, caption, result, picked_by)


def _publish_reel(config: Config, conn: DbConnection, post: Row, token: str, ig_id: str):
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        built = build_weekly_reel(
            config, conn, token, work / "reel.mp4", work_dir=work / "frames",
            ig_id=ig_id,
        )
        conn.execute(
            "UPDATE posts SET caption = ?, credit = ? WHERE id = ?",
            (built.caption, built.track.caption_line() or None, post["id"]),
        )
        conn.commit()
        # **動画も画像と同じ /m/<token> に置く。** Meta が取りに来る。
        # ローカルのファイルを直接送る経路（rupload）は Instagram Login の
        # トークンでは使えない（publish.create_reel_container）。
        # 出し終わったら purge_media が消すので、URLはすぐ死ぬ。
        video_token = media.store_media(
            conn, post["id"], built.video.read_bytes(), mime="video/mp4"
        )
        url = config.instagram.public_media_url(video_token)
        return publish_reel(token, ig_id, url, built.caption)


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


def preview(config: Config, conn: DbConnection, post: Row) -> str:
    """出さずに、何が出るかだけを組み立てて返す。

    最初の1本を出す前に中身を確かめるための経路。画像はここで実際に
    用意する（**取れないことが分かるのは、用意してみたときだけ**）。
    Meta へは一切送らない。
    """
    lines = [f"post_id={post['id']}  {post['kind']}  予定 {post['scheduled_at']}"]
    if post["property_id"]:
        row = conn.execute(
            "SELECT * FROM properties WHERE id = ?", (post["property_id"],)
        ).fetchone()
        if row is not None:
            lines.append(f"物件: {row['title']}（{row['source']}）")
            lines.append(f"代替テキスト: {build_alt_text(row)}")
        square = media.square_bytes(config, conn, post["property_id"], position=1)
        size = media.probe_size(square)
        lines.append(f"画像: {size[0]}x{size[1]}  {len(square) // 1024}KB" if size
                     else "画像: 形式を判定できません")
        if post["kind"] == KIND_FEED:
            count = len(media.available_positions(conn, post["property_id"]))
            count = min(count, config.instagram.carousel_max)
            how = f"カルーセル {count}枚" if count > 1 else "1枚"
            lines.append(f"枚数: {how}（納品と同じ並び）")
    if post["caption"]:
        lines.append("--- 本文 ---")
        lines.append(post["caption"])
    return "\n".join(lines)


@dataclass
class RunResult:
    """1回ぶんの結果。**失敗も返す**（成功数だけだと緑で終わる）。"""

    done: int = 0
    failed: int = 0

    def __bool__(self) -> bool:
        return bool(self.done or self.failed)


def run_once(
    config: Config,
    conn: DbConnection,
    now: datetime | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    kinds: tuple[str, ...] | None = None,
) -> RunResult:
    """時間が来た予定を順に投稿する。

    **戻り値は「出せた件数」と「落ちた件数」の両方。** 成功数だけを返すと、
    呼び出し側が失敗に気づけない。実際 2026-09-01 の初回リールは3回とも
    400 で落ちたのに、GitHub Actions は緑で終わった（誰も見なければ
    「出たはず」で通ってしまう）。

    limit で件数を絞れる。**最初の1本は 1 にして様子を見ること。**
    dry_run なら中身を出して、予定は planned に戻す（消費しない）。

    kinds を渡すとその種別だけを扱う。既定は config の worker_kinds。
    リールは ffmpeg が要るので、審査UI（Render）では担当しない。
    """
    now = now or datetime.now(UTC)
    record = load_token(conn)
    if record is None:
        log.info("Instagram のトークンが未設定のため、投稿は行いません")
        return RunResult()
    if not config.instagram.public_base_url:
        log.warning(
            "instagram.public_base_url が未設定です。"
            "Meta が画像を取りに来られないため、投稿は行いません"
        )
        return RunResult()

    allowed = tuple(kinds if kinds is not None else config.instagram.worker_kinds)
    if not allowed:
        log.info("担当する種別がありません（instagram.worker_kinds が空）")
        return RunResult()
    log.info("担当: %s", " / ".join(allowed))

    ig_id = "（dry-run）" if dry_run else account_id(record.value)
    done = 0
    failed = 0
    while limit is None or done < limit:
        post = claim_due_post(
            conn, now.isoformat(), config.instagram.max_attempts, allowed
        )
        if post is None:
            return RunResult(done, failed)
        # 設定を切り替える前に作られたストーリーズの予定が残っていることが
        # ある。**止めたつもりのものが出る**のが一番まずいので、ここで落とす。
        if post["kind"] == KIND_STORY and not config.instagram.post_story:
            abandon_post(conn, post["id"])
            log.info("自動ストーリーズは無効なので見送りました（post_id=%s）", post["id"])
            continue
        try:
            if dry_run:
                log.info("出しません（dry-run）:\n%s", preview(config, conn, post))
                # 予定は消費しない。戻さないと次回に出なくなる。
                conn.execute(
                    "UPDATE posts SET state = 'planned', attempts = attempts - 1 "
                    "WHERE id = ?", (post["id"],)
                )
                conn.commit()
                done += 1
                continue
            publish_one(config, conn, post, record.value, ig_id)
            done += 1
        except (InstagramError, PostingError, media.MediaError, OSError) as exc:
            state = fail_post(
                conn, post["id"], describe_error(exc), config.instagram.max_attempts
            )
            failed += 1
            log.warning(
                "投稿に失敗しました（post_id=%s / %s）: %s",
                post["id"], "打ち切り" if state == "failed" else "次回に再試行", exc,
            )
    # limit に達してループを抜けた場合。ここが無いと None を返す。
    return RunResult(done, failed)


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
    "WeekPick",
    "WeeklyReel",
    "build_weekly_reel",
    "last_week",
    "weekly_picks",
    "describe_error",
    "preview",
    "publish_one",
    "RunResult",
    "run_once",
]
