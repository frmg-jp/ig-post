"""[9] Instagram への投稿の検証。

実際の Graph API は叩かない。**このコードは一度も本番のAPIに通していない**
ので、ここで固定しているのは「こちら側の組み立てと状態遷移」だけ。
Meta 側の受け付け方は、最初の1本を実際に出して確かめる必要がある。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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
from freming.instagram.plan import _parse_hhmm, next_reel_time, plan, slot_times
from freming.instagram.publish import wait_until_ready
from freming.instagram.tokens import InstagramError

CONFIG = load_config("config.yaml")
NOW = datetime(2026, 8, 10, 1, 0, tzinfo=UTC)  # JST 10:00 月曜


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    return connect(path)


def _property(conn, title="Old Mill House", city="Porto", score=80.0, status="delivered",
              display_name="Old Mill House", caption_body="製粉所の躯体を残した改修住宅です。"):
    """投稿の材料（display_name / caption_body）まで揃った物件。

    揃っていないものは postable_properties が外す（2026-08-22 の事故対応）。
    外れる側を試すテストは display_name=None を渡す。"""
    cursor = conn.execute(
        "INSERT INTO properties (source, source_url, title, location_city, "
        "location_country, summary, score, status, collected_at, "
        "display_name, caption_body) "
        "VALUES ('dezeen', ?, ?, ?, 'Portugal', '製粉所の躯体を残した改修', ?, ?, ?, ?, ?) "
        "RETURNING id",
        (f"https://example.com/{title}", title, city, score, status, NOW.isoformat(),
         display_name, caption_body),
    )
    property_id = cursor.fetchone()["id"]
    conn.commit()
    return property_id


# --- 予定を作る -------------------------------------------------------
def test_枠は設定した時刻ぶんだけ並ぶ():
    """本数は config の post_times に従う。**件数を直書きしない** —
    1日1投稿と3投稿を行き来するので、そのたびに落ちる形にしない。"""
    times = CONFIG.instagram.post_times
    ig = CONFIG.instagram
    local_now = NOW.astimezone(ZoneInfo(ig.timezone))
    today = sum(1 for t in times if _parse_hhmm(t) > local_now.time())

    slots = slot_times(CONFIG, NOW)
    assert len(slots) == today + len(times) * (ig.plan_days - 1)


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
    caption = build_caption(row, CONFIG.caption)
    assert "Old Mill House" in caption
    assert "Porto, Portugal" in caption
    assert "製粉所の躯体を残した改修住宅です。" in caption
    assert "#FremingCurated" in caption


def test_キャプションに価格は入らない(db):
    """通貨がばらばらで為替で見え方が変わるため、投稿には出さない。"""
    property_id = _property(db)
    db.execute("UPDATE properties SET price = '€1,250,000' WHERE id = ?", (property_id,))
    db.commit()
    row = db.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    assert "1,250,000" not in build_caption(row, CONFIG.caption)


def test_音源のクレジットは上限を超えても残る():
    """CC BY の表記を落とすとライセンス違反になる。切るのは本文側。"""
    credit = "familiar by AvapXia — CC BY 4.0"
    caption = with_credit("あ" * 3000, credit)
    assert caption.endswith(credit)
    assert len(caption) <= 2200


def test_リールのキャプションに件数とクレジットが入る():
    caption = build_reel_caption(7, CONFIG.caption, "familiar by AvapXia — CC BY 4.0")
    assert "先週の7件" in caption
    assert caption.endswith("familiar by AvapXia — CC BY 4.0")


def test_クレジット不要なら何も足さない():
    assert "Music:" not in build_reel_caption(7, CONFIG.caption, "")


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


# --- ストーリーズは手で上げる ------------------------------------------
#
# **API は「投稿をストーリーズに追加」を開けていない。** リンク・メンション・
# アンケートといったスタンプは一切投稿できず、キャプションも受け付けない。
# 毎日1回まとめて手で上げる形にしたので、その支度と進捗をここで固定する。


def test_自動ストーリーズは既定で作られない(db):
    _property(db, "one")
    stats = plan(CONFIG, db, NOW)
    assert stats.feed == 1
    assert stats.story == 0


def test_設定を戻せばストーリーズの予定も作られる(db):
    """完全に消してはいない。画像だけのストーリーズが要るなら戻せる。"""
    cfg = CONFIG.model_copy(deep=True)
    cfg.instagram.post_story = True
    _property(db, "one")
    stats = plan(cfg, db, NOW)
    assert stats.story == 1


def test_止めたあとに残った予定は出さずに見送る(db):
    """設定を切り替える前に作られた行が、あとから出てしまうのを防ぐ。"""
    from freming.db.repository import abandon_post

    property_id = _property(db)
    post_id = create_post(db, "story", NOW.isoformat(), property_id=property_id)
    claim_due_post(db, NOW.isoformat(), 3)
    abandon_post(db, post_id)
    row = db.execute("SELECT state FROM posts WHERE id = ?", (post_id,)).fetchone()
    assert row["state"] == "skipped"


def test_手で上げる一覧はその日の公開分だけ(db):
    from freming.db.repository import posts_awaiting_story

    for title, published in [("今日", NOW), ("昨日", NOW - timedelta(days=1))]:
        property_id = _property(db, title)
        post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
        finish_post(db, post_id, f"media-{title}", "c")
        db.execute(
            "UPDATE posts SET published_at = ? WHERE id = ?", (published.isoformat(), post_id)
        )
    db.commit()
    rows = posts_awaiting_story(
        db, NOW.replace(hour=0).isoformat(), (NOW + timedelta(days=1)).isoformat()
    )
    assert [r["title"] for r in rows] == ["今日"]


def test_追加済みの印は付け外しできる(db):
    from freming.db.repository import mark_story_shared, posts_awaiting_story

    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    finish_post(db, post_id, "media-1", "c")
    db.execute("UPDATE posts SET published_at = ? WHERE id = ?", (NOW.isoformat(), post_id))
    db.commit()

    def shared() -> bool:
        rows = posts_awaiting_story(
            db, NOW.replace(hour=0).isoformat(), (NOW + timedelta(days=1)).isoformat()
        )
        return bool(rows[0]["story_shared_at"])

    assert shared() is False
    assert mark_story_shared(db, post_id, True) is True
    assert shared() is True
    assert mark_story_shared(db, post_id, False) is True
    assert shared() is False


def test_未公開には追加済みの印を付けられない(db):
    from freming.db.repository import mark_story_shared

    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    assert mark_story_shared(db, post_id, True) is False


def test_permalinkを保存できる(db):
    from freming.db.repository import set_permalink

    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    set_permalink(db, post_id, "https://www.instagram.com/p/ABC123/")
    row = db.execute("SELECT permalink FROM posts WHERE id = ?", (post_id,)).fetchone()
    assert row["permalink"] == "https://www.instagram.com/p/ABC123/"


def test_permalinkが取れなくても投稿は止まらない():
    """URLは手作業の支度でしかない。取れなくても投稿自体は成立している。"""
    import freming.instagram.publish as publish_mod

    def _deny(*args, **kwargs):
        raise InstagramError("Graph API が 400 を返しました: なんらかの理由")

    original = publish_mod._request
    publish_mod._request = _deny
    try:
        assert publish_mod.media_permalink("t", "m") is None
    finally:
        publish_mod._request = original


# --- 追加した3項目（写真クレジット / 英文 / 代替テキスト）-----------------
def _rich(conn, **extra):
    """仕様欄まで埋まった物件を1件作る。"""
    columns = {
        "usage_type": "Private Residence", "structure": "Post-and-Beam",
        "building_area": "2,008 sq ft", "site_area": "0.34 Acres",
        "style_name": "Mid-Century Modern", "architect": "Richard Lareau",
        "year_built": "1961", "summary_en": "A 1961 post-and-beam house above the canyon.",
        "photo_credit": "Darren Bradley", **extra,
    }
    property_id = _property(conn)
    sets = ", ".join(f"{k} = ?" for k in columns)
    conn.execute(f"UPDATE properties SET {sets} WHERE id = ?",
                 (*columns.values(), property_id))
    conn.commit()
    return conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()


def test_写真のクレジットが入る(db):
    """編集メディアの写真を使わせてもらう以上、出所は書く。"""
    row = _rich(db)
    assert "Photo: Darren Bradley" in build_caption(row, CONFIG.caption, "Dezeen")


def test_撮影者が不明なら媒体名で代える(db):
    row = _rich(db, photo_credit=None)
    assert "Photo: Dezeen" in build_caption(row, CONFIG.caption, "Dezeen")


def test_代替が要らない設定なら出さない(db):
    config = CONFIG.caption.model_copy(update={"photo_credit_fallback_source": False})
    row = _rich(db, photo_credit=None)
    assert "Photo:" not in build_caption(row, config, "Dezeen")


def test_説明文は日本語のみ(db):
    """実運用の投稿（2026-08-19 の4本）に英語の本文は無い。

    英語はリードとCTAと末尾の注記だけ。summary_en を本文に並べない。
    """
    row = _rich(db)
    caption = build_caption(row, CONFIG.caption, "Dezeen")
    assert "A 1961 post-and-beam house" not in caption


def test_審査用の選定理由は公開文に出ない(db):
    """summary は「物語性なし」のような内部評価を含む。**公開文には
    絶対に使わない**（2026-08-22 に実際に公開されてしまった）。"""
    row = _rich(db, caption_body=None)
    caption = build_caption(row, CONFIG.caption, "Dezeen")
    assert row["summary"] not in caption


def test_材料が無い物件は投稿の対象にならない(db):
    """記事が薄くて物件名・説明文を作れなかったものは予定に載せない。
    見出しが住所のまま・本文が審査用の文章のままで出てしまうため。"""
    _property(db, "thin listing", display_name=None, caption_body=None)
    assert postable_properties(db, 10) == []


def test_見出しは短い物件名を優先(db):
    row = _rich(db, display_name="Lareau House")
    caption = build_caption(row, CONFIG.caption, "Dezeen")
    assert "【 Lareau House 】" in caption
    assert f'【 {row["title"]} 】' not in caption


def test_タグは1行に空白区切りで並ぶ(db):
    caption = build_caption(_rich(db), CONFIG.caption, "Dezeen")
    tag_line = caption.rsplit("\n\n", 1)[-1]
    assert tag_line.startswith("#FREMING #FremingCurated")
    assert "\n" not in tag_line
    # 設計者と様式のタグが自動で付く
    assert "#RichardLareau" in tag_line
    assert "#MidCenturyModern" in tag_line


def test_仕様欄に構造と様式の行は出ない(db):
    """実運用の投稿に Structure / Style の行は無い（様式はタグで表す）。"""
    caption = build_caption(_rich(db), CONFIG.caption, "Dezeen")
    assert "Structure:" not in caption
    assert "Style:" not in caption


def test_代替テキストは被写体だけを書く(db):
    """写っているものは分からないので、見えていない細部を作文しない。"""
    from freming.instagram.caption import build_alt_text

    alt = build_alt_text(_rich(db))
    assert "Old Mill House" in alt
    assert "Porto, Portugal" in alt
    assert "Mid-Century Modern" in alt
    assert len(alt) <= 1000


def test_代替テキストは通常投稿にだけ付く():
    """ストーリーズは alt_text もキャプションも受け付けない。"""
    import freming.instagram.publish as publish_mod

    sent = {}

    def _capture(method, url, token, **kwargs):
        sent.update(kwargs.get("params", {}))
        return {"id": "container-1"}

    original = publish_mod._request
    publish_mod._request = _capture
    try:
        publish_mod.create_image_container("t", "1", "https://x/1.jpg", "本文", alt_text="alt")
        assert sent.get("alt_text") == "alt"
        sent.clear()
        publish_mod.create_image_container(
            "t", "1", "https://x/1.jpg", "本文", story=True, alt_text="alt"
        )
        assert "alt_text" not in sent
        assert "caption" not in sent
    finally:
        publish_mod._request = original


# --- 最初の1本を安全に出す ---------------------------------------------
def _token(conn):
    from freming.instagram.tokens import save_token

    save_token(conn, "dummy-token")


def test_件数を絞れる(db, monkeypatch):
    """**最初の1本は 1 にして様子を見る。** まとめて出さない。"""
    from freming.instagram import worker as worker_mod

    _token(db)
    for i in range(3):
        create_post(db, "feed", NOW.isoformat(), property_id=_property(db, f"p{i}"))

    published = []
    monkeypatch.setattr(worker_mod, "account_id", lambda token: "1")
    monkeypatch.setattr(
        worker_mod, "publish_one",
        lambda cfg, conn, post, token, ig_id: published.append(post["id"]),
    )
    cfg = CONFIG.model_copy(deep=True)
    cfg.instagram.public_base_url = "https://example.com"
    assert worker_mod.run_once(cfg, db, NOW, limit=1) == 1
    assert len(published) == 1


def test_dry_runは投稿せず予定も消費しない(db, monkeypatch):
    from freming.instagram import worker as worker_mod

    _token(db)
    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id,
                          caption="本文です")

    def _boom(*args, **kwargs):
        raise AssertionError("dry-run で投稿してはいけない")

    monkeypatch.setattr(worker_mod, "publish_one", _boom)
    monkeypatch.setattr(worker_mod.media, "square_bytes", lambda *a, **k: b"\xff\xd8x")
    cfg = CONFIG.model_copy(deep=True)
    cfg.instagram.public_base_url = "https://example.com"

    assert worker_mod.run_once(cfg, db, NOW, limit=1, dry_run=True) == 1
    row = db.execute("SELECT state, attempts FROM posts WHERE id = ?", (post_id,)).fetchone()
    assert row["state"] == "planned"
    assert row["attempts"] == 0      # 次回そのまま出せる


def test_中身には本文と代替テキストが出る(db, monkeypatch):
    from freming.instagram import worker as worker_mod

    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id,
                          caption="ここが本文")
    monkeypatch.setattr(worker_mod.media, "square_bytes", lambda *a, **k: b"\xff\xd8x")
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    text = worker_mod.preview(CONFIG, db, post)
    assert "ここが本文" in text
    assert "Old Mill House" in text


# --- 動かす場所を分ける -------------------------------------------------
#
# リールは ffmpeg と数百MBのメモリが要るので、審査UI（Render の無料プラン）
# では作れない。GitHub Actions に逃がす。**リールの動画は公開URLを使わない**
# （rupload へ直接送る）ので、画像を配れない場所でも投稿できる。


def test_担当しない種別は取らない(db):
    property_id = _property(db)
    create_post(db, "reel", NOW.isoformat())
    create_post(db, "feed", NOW.isoformat(), property_id=property_id)

    got = claim_due_post(db, NOW.isoformat(), 3, ("feed", "story"))
    assert got["kind"] == "feed"
    # リールは残っている（別の場所が取る）
    assert claim_due_post(db, NOW.isoformat(), 3, ("reel",))["kind"] == "reel"


def test_種別を指定しなければ全部取る(db):
    create_post(db, "reel", NOW.isoformat())
    assert claim_due_post(db, NOW.isoformat(), 3)["kind"] == "reel"


def test_審査UIの既定はリールを含まない():
    """Render では作れないので、既定で担当させない。"""
    assert "reel" not in CONFIG.instagram.worker_kinds
    assert "feed" in CONFIG.instagram.worker_kinds


def test_担当が空なら何もしない(db, monkeypatch):
    from freming.instagram import worker as worker_mod

    _token(db)
    create_post(db, "feed", NOW.isoformat(), property_id=_property(db))
    cfg = CONFIG.model_copy(deep=True)
    cfg.instagram.public_base_url = "https://example.com"
    cfg.instagram.worker_kinds = []
    monkeypatch.setattr(worker_mod, "account_id", lambda token: "1")
    assert worker_mod.run_once(cfg, db, NOW) == 0


# --- 予定の本文を作り直す ----------------------------------------------
#
# 本文は**予定を作った時点**で組んで持っている。あとから項目を足しても
# 既存の予定には反映されないので、出す前に作り直せるようにしてある。


def test_出していない予定の本文は作り直せる(db):
    from freming.db.repository import planned_posts_with_property, set_caption

    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id,
                          caption="古い本文")
    rows = planned_posts_with_property(db)
    assert [r["post_id"] for r in rows] == [post_id]
    assert set_caption(db, post_id, "新しい本文") is True
    row = db.execute("SELECT caption FROM posts WHERE id = ?", (post_id,)).fetchone()
    assert row["caption"] == "新しい本文"


def test_投稿済みの本文は作り直さない(db):
    """出したあとの本文を書き換えても実物は変わらない。履歴として残す。"""
    from freming.db.repository import planned_posts_with_property, set_caption

    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id,
                          caption="出した本文")
    finish_post(db, post_id, "media-1", "c")
    assert planned_posts_with_property(db) == []
    assert set_caption(db, post_id, "書き換え") is False
    row = db.execute("SELECT caption FROM posts WHERE id = ?", (post_id,)).fetchone()
    assert row["caption"] == "出した本文"


def test_失敗した予定は作り直しの対象になる(db):
    """本文が原因で弾かれた場合、直して再挑戦できる。"""
    from freming.db.repository import planned_posts_with_property

    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    claim_due_post(db, NOW.isoformat(), 1)
    fail_post(db, post_id, "だめ", 1)
    assert [r["post_id"] for r in planned_posts_with_property(db)] == [post_id]


# --- 溜まった予定を先送りする -----------------------------------------
def test_過ぎた予定は引ける(db):
    """ワーカーが止まっていた間に溜まった行を、まとめて動かすために引く。"""
    from freming.db.repository import stale_planned_posts

    property_id = _property(db)
    old = create_post(db, "feed", (NOW - timedelta(days=2)).isoformat(),
                      property_id=property_id)
    other = _property(db, "Future House")
    create_post(db, "feed", (NOW + timedelta(hours=3)).isoformat(), property_id=other)

    rows = stale_planned_posts(db, NOW.isoformat())
    assert [row["id"] for row in rows] == [old]


def test_出したあとの予定は先送りの対象にならない(db):
    """**投稿済みを動かすと、同じものがもう一度出る。**"""
    from freming.db.repository import set_scheduled_at, stale_planned_posts

    property_id = _property(db)
    post_id = create_post(db, "feed", (NOW - timedelta(days=1)).isoformat(),
                          property_id=property_id)
    claim_due_post(db, NOW.isoformat(), 3)
    finish_post(db, post_id, "media-1", "c")

    assert stale_planned_posts(db, NOW.isoformat()) == []
    assert set_scheduled_at(db, post_id, NOW.isoformat()) is False


def test_先送りしても物件は候補から外れたまま(db):
    """捨てずに動かす理由。**消すと二度と投稿候補に戻らない。**"""
    from freming.db.repository import set_scheduled_at

    property_id = _property(db)
    post_id = create_post(db, "feed", (NOW - timedelta(days=1)).isoformat(),
                          property_id=property_id)
    assert set_scheduled_at(db, post_id, (NOW + timedelta(hours=5)).isoformat()) is True
    assert [row["id"] for row in postable_properties(db, 10)] == []


# --- 2026-08-19 の変更: 1行目の「・」・Location の州・㎡併記・カルーセル ---
def test_一行目は中黒でリードは2行目から(db):
    """「・」とリードの間に空行を挟まない（2026-08-22 の指示）。"""
    caption = build_caption(_rich(db), CONFIG.caption, "Dezeen")
    head = caption.split("\n")
    assert head[0] == "・"
    assert head[1].startswith("世界で今、")


def test_リールも一行目は中黒でリードは2行目から():
    """リードは通常投稿と別。**リールは1週間のまとめ**なので文言が違う。"""
    caption = build_reel_caption(7, CONFIG.caption)
    head = caption.split("\n")
    assert head[0] == "・"
    assert head[1] == CONFIG.caption.reel.lead_reach[0].replace("{count}", "7")
    # 名前を渡さなかったときの逃げ道。件数だけは出す
    assert "【 先週の7件 】" in caption


def test_Locationに州が入りUSAに縮める(db):
    row = _rich(db, location_region="California")
    db.execute("UPDATE properties SET location_city='Pasadena', "
               "location_country='United States' WHERE id=?", (row["id"],))
    db.commit()
    row = db.execute("SELECT * FROM properties WHERE id=?", (row["id"],)).fetchone()
    caption = build_caption(row, CONFIG.caption, "Dezeen")
    assert "Location: Pasadena, California, USA" in caption


def test_面積に平米の併記が付く(db):
    row = _rich(db, building_area="1,962 sq ft", site_area="2 Acres")
    caption = build_caption(row, CONFIG.caption, "Dezeen")
    assert "Building Area: 1,962 sq ft (Approx. 182㎡)" in caption
    assert "Site Area: 2 Acres (Approx. 8,094㎡)" in caption


def test_既に平米がある面積は触らない(db):
    row = _rich(db, building_area="713 sq ft (Approx. 66㎡)")
    caption = build_caption(row, CONFIG.caption, "Dezeen")
    assert caption.count("66㎡") == 1
    assert "66㎡ (Approx." not in caption


def test_カルーセルの子コンテナはキャプションを持たない(monkeypatch):
    from freming.instagram import publish

    calls = []

    def fake_request(method, url, token, **kwargs):
        calls.append(kwargs.get("params", {}))
        return {"id": f"c{len(calls)}"}

    monkeypatch.setattr(publish, "_request", fake_request)
    monkeypatch.setattr(publish, "wait_until_ready", lambda *a, **k: None)
    result = publish.publish_carousel(
        "t", "ig1", ["https://e/1", "https://e/2"], "本文", alt_text="alt"
    )
    assert result.media_id == "c4"  # 子2 + 親1 + publish1
    child1, child2, parent, published = calls
    assert child1["is_carousel_item"] == "true" and "caption" not in child1
    assert child1["alt_text"] == "alt"
    assert parent["media_type"] == "CAROUSEL"
    assert parent["children"] == "c1,c2"
    assert parent["caption"] == "本文"
    assert published["creation_id"] == "c3"


def test_カルーセルは1枚では作らない():
    from freming.instagram import publish

    with pytest.raises(InstagramError):
        publish.publish_carousel("t", "ig1", ["https://e/1"], "本文")


def test_公開済みの投稿を予定に戻せる(db, monkeypatch, tmp_path):
    """IG側で消した投稿の出し直し。物件は候補に出ないままがよい
    （postsの行は残るので postable からは外れ続ける）。"""
    property_id = _property(db)
    post_id = create_post(db, "feed", NOW.isoformat(), property_id=property_id)
    claim_due_post(db, NOW.isoformat(), 3)
    finish_post(db, post_id, "media-1", "c1")

    db.execute(
        "UPDATE posts SET state='planned', ig_media_id=NULL, attempts=0, "
        "published_at=NULL WHERE id=?", (post_id,))
    db.commit()
    row = db.execute("SELECT state FROM posts WHERE id=?", (post_id,)).fetchone()
    assert row["state"] == "planned"
    assert [r["id"] for r in postable_properties(db, 10)] == []


def test_クレジットは仕様欄の最終行に入る(db):
    """Photo: は Built in: の直下。独立した段落にしない（2026-08-22）。"""
    caption = build_caption(_rich(db), CONFIG.caption, "Dezeen")
    assert "Built in: 1961\nPhoto: Darren Bradley" in caption
    assert "\n\nPhoto:" not in caption


# --- リールの本文に物件名を並べる（2026-08-31） ------------------------
#
# 名前が無いと「先週の7件」の1行だけで、何が映るのか読む側に伝わらない。

_REEL_NAMES = ["Kip House", "Java 209, Est. 1965", "The Hangover House"]


def test_リールの本文に物件名が並ぶ():
    caption = build_reel_caption(3, CONFIG.caption, names=_REEL_NAMES)
    for name in _REEL_NAMES:
        assert name in caption
    # 番号は動画の並びと対応させる
    assert "1  Kip House" in caption
    assert "3  The Hangover House" in caption


def test_名前が並ぶときは件数の行を出さない():
    caption = build_reel_caption(3, CONFIG.caption, names=_REEL_NAMES)
    assert "【 先週の3件 】" not in caption


def test_名前が空なら件数の行に戻る():
    """1つも取れなかったときの逃げ道。**件数だけは出す。**"""
    caption = build_reel_caption(3, CONFIG.caption, names=["", "  ", None])
    assert "【 先週の3件 】" in caption


def test_リーチで選べたときだけ見られたと書く():
    """**直近で代用したときに「いちばん見られた」と書くと嘘になる。**"""
    by_reach = build_reel_caption(3, CONFIG.caption, names=_REEL_NAMES,
                                  picked_by="reach")
    by_recent = build_reel_caption(3, CONFIG.caption, names=_REEL_NAMES,
                                   picked_by="recent")
    assert "見られた" in by_reach
    assert "見られた" not in by_recent
    assert by_recent.split("\n")[1] == CONFIG.caption.reel.lead_recent[0].replace(
        "{count}", "3"
    )


def test_名前が並んでもクレジットは末尾に残る():
    """CC BY は表記が要件。長くなっても落とさない。"""
    caption = build_reel_caption(
        7, CONFIG.caption, "familiar by AvapXia — CC BY 4.0",
        names=["あ" * 300] * 7,
    )
    assert caption.endswith("familiar by AvapXia — CC BY 4.0")
    assert len(caption) <= 2200
