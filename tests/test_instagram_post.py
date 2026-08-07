"""[9] Instagram への投稿の検証。

実際の Graph API は叩かない。**このコードは一度も本番のAPIに通していない**
ので、ここで固定しているのは「こちら側の組み立てと状態遷移」だけ。
Meta 側の受け付け方は、最初の1本を実際に出して確かめる必要がある。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image

from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import (
    claim_due_post,
    create_post,
    fail_post,
    finish_post,
    postable_properties,
    published_posts_between,
    record_reach,
    retry_post,
    scheduled_posts,
    skip_post,
)
from freming.instagram import media
from freming.instagram.caption import build_caption, build_reel_caption, with_credit
from freming.instagram.insights import MissingInsightsScope, media_reach
from freming.instagram.plan import next_reel_time, plan, slot_times
from freming.instagram.publish import wait_until_ready
from freming.instagram.tokens import InstagramError

CONFIG = load_config("config.yaml")
NOW = datetime(2026, 8, 10, 1, 0, tzinfo=UTC)  # JST 10:00 月曜


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    return connect(path)


def _property(conn, title="Old Mill House", city="Porto", score=80.0, status="delivered"):
    cursor = conn.execute(
        "INSERT INTO properties (source, source_url, title, location_city, "
        "location_country, summary, score, status, collected_at) "
        "VALUES ('dezeen', ?, ?, ?, 'Portugal', '製粉所の躯体を残した改修', ?, ?, ?) "
        "RETURNING id",
        (f"https://example.com/{title}", title, city, score, status, NOW.isoformat()),
    )
    property_id = cursor.fetchone()["id"]
    conn.commit()
    return property_id


# --- 予定を作る -------------------------------------------------------
def test_枠は設定した時刻ぶんだけ並ぶ():
    slots = slot_times(CONFIG, NOW)
    # 当日の 13:00 と 20:00（09:00 は過ぎている）＋ 翌日以降 2日ぶん
    assert len(slots) == 2 + 3 * (CONFIG.instagram.plan_days - 1)


def test_過ぎた時刻の枠は作らない():
    late = NOW.replace(hour=12)  # JST 21:00
    assert all(s > late for s in slot_times(CONFIG, late))


def test_リールは指定した曜日に来る():
    moment = next_reel_time(CONFIG, NOW)
    from zoneinfo import ZoneInfo

    local = moment.astimezone(ZoneInfo(CONFIG.instagram.timezone))
    assert local.weekday() == CONFIG.instagram.reel_weekday
    assert moment > NOW


def test_納品済みだけが投稿の対象になる(db):
    _property(db, "delivered one", status="delivered")
    _property(db, "approved only", status="approved")
    rows = postable_properties(db, 10)
    assert [r["title"] for r in rows] == ["delivered one"]


def test_一度投稿した物件は次の対象に出ない(db):
    property_id = _property(db)
    create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    assert postable_properties(db, 10) == []


def test_同じ物件の同じ種別は二度作られない(db):
    property_id = _property(db)
    assert create_post(db, "feed", NOW.isoformat(), property_id=property_id) is not None
    assert create_post(db, "feed", NOW.isoformat(), property_id=property_id) is None


def test_通常投稿とストーリーズは別に作れる(db):
    property_id = _property(db)
    assert create_post(db, "feed", NOW.isoformat(), property_id=property_id) is not None
    assert create_post(db, "story", NOW.isoformat(), property_id=property_id) is not None


def test_在庫が足りない日は枠を空ける(db):
    _property(db, "only one")
    stats = plan(CONFIG, db, NOW)
    assert stats.feed == 1
    assert stats.short_of_stock > 0


def test_予定を作り直しても重複しない(db):
    _property(db, "one")
    first = plan(CONFIG, db, NOW)
    second = plan(CONFIG, db, NOW)
    assert first.feed == 1
    assert second.feed == 0


# --- 状態遷移 ---------------------------------------------------------
def test_時間が来ていない予定は取れない(db):
    property_id = _property(db)
    create_post(db, "feed", (NOW + timedelta(hours=2)).isoformat(), property_id=property_id)
    assert claim_due_post(db, NOW.isoformat(), 3) is None


def test_同じ予定を二度は取れない(db):
    """2箇所でワーカーが動いても二重投稿にならないこと。"""
    property_id = _property(db)
    create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    first = claim_due_post(db, NOW.isoformat(), 3)
    second = claim_due_post(db, NOW.isoformat(), 3)
    assert first is not None
    assert second is None


def test_失敗は上限まで予定に戻る(db):
    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    for _ in range(2):
        claim_due_post(db, NOW.isoformat(), 3)
        assert fail_post(db, post_id, "だめ", 3) == "planned"
    claim_due_post(db, NOW.isoformat(), 3)
    assert fail_post(db, post_id, "だめ", 3) == "failed"
    # 打ち切ったあとは拾われない
    assert claim_due_post(db, NOW.isoformat(), 3) is None


def test_見送ると投稿されない(db):
    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    assert skip_post(db, post_id) is True
    assert claim_due_post(db, NOW.isoformat(), 3) is None
    assert retry_post(db, post_id) is True
    assert claim_due_post(db, NOW.isoformat(), 3) is not None


def test_投稿済みは見送れない(db):
    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    finish_post(db, post_id, "media-1", "container-1")
    assert skip_post(db, post_id) is False


def test_予定表には時刻順に並ぶ(db):
    a = _property(db, "a")
    b = _property(db, "b")
    create_post(db, "feed", (NOW + timedelta(hours=3)).isoformat(), property_id=a)
    create_post(db, "feed", (NOW + timedelta(hours=1)).isoformat(), property_id=b)
    rows = scheduled_posts(db, (NOW + timedelta(days=1)).isoformat())
    assert [r["property_id"] for r in rows] == [b, a]


# --- 配る画像 ---------------------------------------------------------
def test_置いた画像は_tokenで引ける(db):
    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    token = media.store_media(db, post_id, b"\xff\xd8dummy")
    assert media.load_media(db, token) == (b"\xff\xd8dummy", "image/jpeg")


def test_tokenは毎回変わる():
    assert media.new_token() != media.new_token()
    assert len(media.new_token()) > 20


def test_投稿が済んだ画像は消える(db):
    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    token = media.store_media(db, post_id, b"data")
    assert media.purge_media(db, post_id) == 1
    assert media.load_media(db, token) is None


def test_知らない_tokenは何も返さない(db):
    assert media.load_media(db, "ないよ") is None


def test_ストーリーズ用に縦へ組み替わる(tmp_path):
    square = Image.new("RGB", (1080, 1080), "#334455")
    path = tmp_path / "s.jpg"
    square.save(path, "JPEG")
    vertical = media.to_vertical(path.read_bytes(), CONFIG)
    assert media.probe_size(vertical) == (1080, 1920)


def test_公開URLが未設定なら理由が分かる形で落ちる():
    from freming.config import InstagramConfig

    with pytest.raises(ValueError, match="public_base_url"):
        InstagramConfig().public_media_url("abc")


def test_公開URLはtokenを付けて返る():
    from freming.config import InstagramConfig

    config = InstagramConfig(public_base_url="https://example.com/")
    assert config.public_media_url("abc") == "https://example.com/m/abc"


# --- キャプション -----------------------------------------------------
def test_キャプションに場所と選定理由が入る(db):
    property_id = _property(db)
    row = db.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    caption = build_caption(row, ["frmg"])
    assert "Old Mill House" in caption
    assert "Porto, Portugal" in caption
    assert "製粉所の躯体を残した改修" in caption
    assert "#frmg" in caption


def test_キャプションに価格は入らない(db):
    """通貨がばらばらで為替で見え方が変わるため、投稿には出さない。"""
    property_id = _property(db)
    db.execute("UPDATE properties SET price = '€1,250,000' WHERE id = ?", (property_id,))
    db.commit()
    row = db.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    assert "1,250,000" not in build_caption(row)


def test_音源のクレジットは上限を超えても残る():
    """CC BY の表記を落とすとライセンス違反になる。切るのは本文側。"""
    credit = "familiar by AvapXia — CC BY 4.0"
    caption = with_credit("あ" * 3000, credit)
    assert caption.endswith(credit)
    assert len(caption) <= 2200


def test_リールのキャプションに件数とクレジットが入る():
    caption = build_reel_caption(7, ["frmg"], "familiar by AvapXia — CC BY 4.0")
    assert "今週の7件" in caption
    assert caption.endswith("familiar by AvapXia — CC BY 4.0")


def test_クレジット不要なら何も足さない():
    assert "Music:" not in build_reel_caption(7, [], "")


# --- コンテナの待ち ---------------------------------------------------
def test_仕上がるまで待つ():
    states = iter(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    calls = []
    import freming.instagram.publish as publish_mod

    original = publish_mod.container_status
    publish_mod.container_status = lambda token, cid: next(states)
    try:
        wait_until_ready("t", "c", sleep=calls.append, now=lambda: 0.0)
    finally:
        publish_mod.container_status = original
    assert len(calls) == 2


def test_コンテナがERRORなら理由が分かる形で落ちる():
    import freming.instagram.publish as publish_mod

    original = publish_mod.container_status
    publish_mod.container_status = lambda token, cid: "ERROR"
    try:
        with pytest.raises(InstagramError, match="ERROR"):
            wait_until_ready("t", "c", sleep=lambda _: None, now=lambda: 0.0)
    finally:
        publish_mod.container_status = original


def test_仕上がらないまま時間切れなら落ちる():
    import freming.instagram.publish as publish_mod

    clock = iter([0.0, 1000.0])
    original = publish_mod.container_status
    publish_mod.container_status = lambda token, cid: "IN_PROGRESS"
    try:
        with pytest.raises(InstagramError, match="秒たっても"):
            wait_until_ready("t", "c", sleep=lambda _: None, now=lambda: next(clock))
    finally:
        publish_mod.container_status = original


# --- リーチ -----------------------------------------------------------
def test_権限が無ければ再認可を促す():
    """黙って迂回しない。スコープ不足はそう言う。"""
    import freming.instagram.insights as insights_mod

    def _deny(*args, **kwargs):
        raise InstagramError("Graph API が 400 を返しました: (#10) permission denied")

    original = insights_mod._request
    insights_mod._request = _deny
    try:
        with pytest.raises(MissingInsightsScope, match="auth-url"):
            media_reach("t", "m")
    finally:
        insights_mod._request = original


def test_リーチはあとから書き込める(db):
    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    finish_post(db, post_id, "media-1", "container-1")
    # published_at は実時刻で入る。期間の検証をしたいので明示的に置き直す。
    db.execute("UPDATE posts SET published_at = ? WHERE id = ?", (NOW.isoformat(), post_id))
    db.commit()
    record_reach(db, post_id, 1234)
    rows = published_posts_between(
        db, (NOW - timedelta(days=1)).isoformat(), (NOW + timedelta(days=1)).isoformat()
    )
    assert [r["reach"] for r in rows] == [1234]


def test_各日の1位が選ばれる(db):
    """日をまたいで比べない。リーチは時間とともに伸びるため。"""
    from freming.instagram.worker import daily_winners

    for day, (title, reach) in enumerate(
        [("d1-low", 10), ("d1-high", 90), ("d2-low", 5), ("d2-high", 50)]
    ):
        property_id = _property(db, title)
        post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
        finish_post(db, post_id, f"media-{day}", "c")
        published = NOW - timedelta(days=1 if day < 2 else 2)
        db.execute(
            "UPDATE posts SET published_at = ?, reach = ? WHERE id = ?",
            (published.isoformat(), reach, post_id),
        )
        db.commit()

    winners = daily_winners(CONFIG, db, "token", NOW)
    titles = []
    for row in winners:
        found = db.execute(
            "SELECT title FROM properties WHERE id = ?", (row["property_id"],)
        ).fetchone()
        titles.append(found["title"])
    assert sorted(titles) == ["d1-high", "d2-high"]
