"""[9] 何をいつ投稿するかを決める。

予定を**先に行として作る**。投稿の直前に決めるのではなく3日先まで並べる
のは、人が見て止められるようにするため（審査UIの /schedule）。承認が
そのまま公開になる作りにすると、MLS のロゴ入りの写真や、いまいちな1枚を
誰もチェックできない。

決め方:

  - 対象は**納品済み**の物件だけ。承認しただけで画像が揃わなかったものは
    投稿に回さない（納品まで通った＝画像がある、という保証を使う）
  - スコアの高い順。同点なら新しい順
  - 1日 `post_times` の数だけ。既に予定がある枠は飛ばす
  - 在庫が足りない日は**枠を空ける**。埋めるために基準を下げない

時刻は config の timezone（既定 Asia/Tokyo）で解釈し、DBには UTC で持つ。
DBに現地時刻を混ぜると、環境によってずれる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from freming.config import Config
from freming.db.connection import DbConnection
from freming.db.repository import (
    create_post,
    postable_properties,
    scheduled_posts,
)
from freming.instagram.caption import build_caption
from freming.instagram.publish import KIND_FEED, KIND_REEL, KIND_STORY
from freming.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class PlanStats:
    feed: int = 0
    story: int = 0
    reel: int = 0
    short_of_stock: int = 0
    slots: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"予定を作りました: 通常 {self.feed} / ストーリーズ {self.story} / リール {self.reel}"
        if self.short_of_stock:
            text += f"（在庫不足で {self.short_of_stock} 枠は空けました）"
        return text


def _zone(config: Config) -> ZoneInfo:
    return ZoneInfo(config.instagram.timezone)


def _parse_hhmm(text: str) -> time:
    hour, _, minute = text.partition(":")
    return time(int(hour), int(minute or 0))


def slot_times(config: Config, now: datetime) -> list[datetime]:
    """これから `plan_days` 日ぶんの投稿枠を UTC で並べる。

    今より前の時刻は入れない。当日の途中で走らせても、過ぎた枠を
    さかのぼって作らない。
    """
    zone = _zone(config)
    local_now = now.astimezone(zone)
    out: list[datetime] = []
    for offset in range(config.instagram.plan_days):
        day = (local_now + timedelta(days=offset)).date()
        for text in config.instagram.post_times:
            moment = datetime.combine(day, _parse_hhmm(text), tzinfo=zone)
            if moment > local_now:
                out.append(moment.astimezone(UTC))
    return sorted(out)


def next_reel_time(config: Config, now: datetime) -> datetime:
    """次のリールの時刻。reel_weekday は 0=月曜。"""
    zone = _zone(config)
    local_now = now.astimezone(zone)
    target = _parse_hhmm(config.instagram.reel_time)
    ahead = (config.instagram.reel_weekday - local_now.weekday()) % 7
    moment = datetime.combine(local_now.date() + timedelta(days=ahead), target, tzinfo=zone)
    if moment <= local_now:
        moment += timedelta(days=7)
    return moment.astimezone(UTC)


def _taken_slots(conn: DbConnection, until: datetime) -> set[str]:
    rows = scheduled_posts(conn, until.isoformat())
    return {row["scheduled_at"] for row in rows if row["kind"] == KIND_FEED}


def plan(config: Config, conn: DbConnection, now: datetime | None = None) -> PlanStats:
    """予定を埋める。既にある予定は触らない。"""
    now = now or datetime.now(UTC)
    stats = PlanStats()
    ig = config.instagram

    slots = slot_times(config, now)
    if not slots:
        return stats
    taken = _taken_slots(conn, slots[-1] + timedelta(minutes=1))
    empty = [s for s in slots if s.isoformat() not in taken]
    if not empty:
        return stats

    sources = ig.allowed_sources or None
    candidates = list(postable_properties(conn, len(empty), sources))
    if len(candidates) < len(empty):
        stats.short_of_stock = len(empty) - len(candidates)

    for moment, row in zip(empty, candidates, strict=False):
        # 撮影者が記事から取れなかったときは媒体名で代える。
        src = config.editorial_source(row["source"]) or config.listing_source(row["source"])
        caption = build_caption(row, config.caption, src.name if src else None)
        post_id = create_post(
            conn, KIND_FEED, moment.isoformat(), property_id=row["id"], caption=caption
        )
        if post_id is None:
            continue
        stats.feed += 1
        stats.slots.append(moment.isoformat())

        if ig.post_story:
            story_at = moment + timedelta(minutes=ig.story_delay_min)
            if create_post(
                conn, KIND_STORY, story_at.isoformat(),
                property_id=row["id"], parent_post_id=post_id,
            ) is not None:
                stats.story += 1

    if ig.post_reel:
        stats.reel += _plan_reel(config, conn, now)

    log.info("%s", stats.summary())
    return stats


def _plan_reel(config: Config, conn: DbConnection, now: datetime) -> int:
    """次の週次リールを1件だけ置く。既に置いてあれば何もしない。"""
    moment = next_reel_time(config, now)
    existing = conn.execute(
        "SELECT id FROM posts WHERE kind = ? AND scheduled_at = ?",
        (KIND_REEL, moment.isoformat()),
    ).fetchone()
    if existing is not None:
        return 0
    # キャプションは中身が決まってから（何件入ったか・どの音源か）組む。
    return 1 if create_post(conn, KIND_REEL, moment.isoformat()) is not None else 0


def compact(config: Config, conn: DbConnection, now: datetime | None = None) -> int:
    """空いた枠を詰める。見送り・削除で穴が空いたときに使う。

    **順番は変えない。** これから出る通常投稿を時系列のまま、先頭の
    空き枠から順に詰め直すだけ。動いた件数を返す。

      - 公開済み・投稿中は動かさず、その枠も使わない（**同じ日に2本
        並ぶのを防ぐ**）
      - 見送りは枠を持たない。予定を詰めたあとの後ろへ寄せる
      - リールは曜日が決まっているので対象外
    """
    from freming.db.repository import set_scheduled_at

    now = now or datetime.now(UTC)
    slots = slot_times(config, now)
    if not slots:
        return 0

    until = slots[-1] + timedelta(minutes=1)
    upcoming = [
        row for row in scheduled_posts(conn, until.isoformat())
        if row["kind"] == KIND_FEED and row["scheduled_at"] > now.isoformat()
    ]

    # **既に出たもの・出している最中のものが居る枠は使わない。**
    # ここを見落とすと、その日に2本並ぶ。
    taken = {
        row["scheduled_at"] for row in upcoming
        if row["state"] in ("published", "publishing")
    }
    free = [s for s in slots if s.isoformat() not in taken]

    rows = sorted(
        (row for row in upcoming if row["state"] == "planned"),
        key=lambda r: (r["scheduled_at"], r["id"]),
    )
    # 見送りは枠を持たない。**出さないと決めたものが枠を塞ぐと、
    # その日に2本並ぶ（2026-08-22 に実際に起きた）。** 予定を詰めた
    # あとの、いちばん後ろの枠へ寄せる。
    parked = [row for row in upcoming if row["state"] == "skipped"]

    moved = 0
    for row, moment in zip(rows, free, strict=False):
        if row["scheduled_at"] == moment.isoformat():
            continue
        if set_scheduled_at(conn, row["id"], moment.isoformat()):
            moved += 1

    tail = free[len(rows):] or [slots[-1]]
    for index, row in enumerate(parked):
        moment = tail[min(index, len(tail) - 1)]
        if row["scheduled_at"] == moment.isoformat():
            continue
        if set_scheduled_at(conn, row["id"], moment.isoformat()):
            moved += 1

    if moved:
        log.info("%d 件を詰めました", moved)
    return moved


__all__ = ["PlanStats", "compact", "next_reel_time", "plan", "slot_times"]
