"""[9] 週次リールの検証。

ffmpeg が要るのは動画を書き出すところだけ。コマの組み立てと音源の
割り当ては ffmpeg 無しで検証できるので、CI でも必ず通る。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from freming.config import ReelConfig, load_config
from freming.reel.build import (
    ReelError,
    audio_for_week,
    build_reel,
    compose_frame,
    load_tracks,
)

CONFIG = load_config("config.yaml")
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg が無い環境ではスキップする"
)


@pytest.fixture()
def square(tmp_path):
    """納品画像と同じ 1080x1080 を1枚作る。上下で色を変えて向きを見る。"""
    img = Image.new("RGB", (1080, 1080), "#204060")
    img.paste(Image.new("RGB", (1080, 540), "#c08040"), (0, 0))
    path = tmp_path / "01.jpg"
    img.save(path, "JPEG", quality=95)
    return path


# --- コマの組み立て ---------------------------------------------------
def test_コマは縦1080x1920になる(tmp_path, square):
    dest = tmp_path / "frame.jpg"
    compose_frame(square, dest, CONFIG.reel)
    assert Image.open(dest).size == (1080, 1920)


def test_正方形は中央より上に置かれる(tmp_path, square):
    """Reels は下側にUIが重なるので、真ん中に置かない。

    前景と背景は明るさで見分ける（背景は bg_brightness で暗くしてある）。
    上端・下端の内側と外側を比べれば、置いた位置がそのまま出る。
    """
    dest = tmp_path / "frame.jpg"
    compose_frame(square, dest, CONFIG.reel)
    top = (1920 - 1080) // 2 - CONFIG.reel.square_offset_px
    with Image.open(dest) as frame:
        def brightness(y):
            return sum(frame.getpixel((540, y)))

        assert brightness(top + 5) > brightness(top - 5)          # 上端
        assert brightness(top + 1080 - 5) > brightness(top + 1080 + 5)  # 下端
        # 前景の上端は元画像の上半分（#c08040）がそのまま出る
        assert frame.getpixel((540, top + 5)) == pytest.approx((192, 128, 64), abs=8)


def test_背景は暗く沈む(tmp_path, square):
    """前景を立たせるために背景は暗くする。同じ色のままだと境目が消える。"""
    dest = tmp_path / "frame.jpg"
    compose_frame(square, dest, CONFIG.reel)
    with Image.open(dest) as frame:
        background = sum(frame.getpixel((540, 40)))
        foreground = sum(frame.getpixel((540, 500)))
    assert background < foreground


def test_寄らない設定でも組める(tmp_path, square):
    config = ReelConfig(zoom=0.0)
    dest = tmp_path / "frame.jpg"
    compose_frame(square, dest, config)
    assert Image.open(dest).size == (1080, 1920)


# --- 音源の割り当て ---------------------------------------------------
def test_同じ週なら何度でも同じ曲になる():
    """作り直しても音が変わらないこと。剰余で選んでいる理由。"""
    assert audio_for_week(3).path == audio_for_week(3).path


def test_週が進むと曲が一巡する():
    tracks = load_tracks()
    assert audio_for_week(0).title == audio_for_week(len(tracks)).title


def test_クレジットが要る曲は本文を持つ():
    for track in load_tracks():
        if track.attribution_required:
            assert track.credit
            assert track.artist in track.credit
            assert track.license in track.credit
        else:
            assert track.caption_line() == ""


def test_音源のファイルが実在する():
    for track in load_tracks():
        assert track.path.exists(), f"{track.title} の実体がありません"


def test_音源の一覧が無ければ理由が分かる形で落ちる(tmp_path):
    with pytest.raises(ReelError, match="見つかりません"):
        load_tracks(tmp_path / "none.json")


def test_manifestの週順は連番():
    """順番に穴があると、その週だけ曲が飛ぶ。"""
    raw = json.loads(Path("assets/audio/manifest.json").read_text(encoding="utf-8"))
    orders = sorted(row["order"] for row in raw["tracks"])
    assert orders == list(range(1, len(orders) + 1))


# --- 動画 -------------------------------------------------------------
@needs_ffmpeg
def test_尺は枚数が変わっても指定どおり(tmp_path, square):
    """繋ぎ目のぶん1枚を長くしているので、全体は必ず total_sec に収まる。"""
    squares = []
    for i in range(3):
        path = tmp_path / f"{i}.jpg"
        shutil.copy(square, path)
        squares.append(path)

    config = ReelConfig(total_sec=6.0, crf=30, x264_preset="ultrafast")
    dest = tmp_path / "out.mp4"
    result = build_reel(squares, audio_for_week(0), dest, config)

    assert dest.exists()
    assert result.image_count == 3
    probed = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(dest)],
        capture_output=True, text=True,
    ).stdout
    assert float(probed) == pytest.approx(6.0, abs=0.15)


@needs_ffmpeg
def test_音声が必ず入る(tmp_path, square):
    """無音のまま書き出して気づかない、が一番まずい。"""
    config = ReelConfig(total_sec=4.0, crf=30, x264_preset="ultrafast")
    dest = tmp_path / "out.mp4"
    build_reel([square], audio_for_week(0), dest, config)

    codecs = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(dest)],
        capture_output=True, text=True,
    ).stdout.strip()
    assert codecs == "aac"


def test_画像が無ければ作らない(tmp_path):
    with pytest.raises(ReelError, match="画像が1枚も"):
        build_reel([], audio_for_week(0), tmp_path / "out.mp4", CONFIG.reel)


# ----------------------------------------------------------------------
# 週次リールの選定と組み立て（2026-08-31）
#
# 出す処理と試写が**同じ関数**を通ることが要点。別々に書くと、試写で
# 見たものと出るものが食い違う。食い違う試写は無いより悪い。
# ----------------------------------------------------------------------

# 週次リールが走る想定の時刻。**壁時計を使わない。** 選抜は暦の週
# （先週の月〜日）で切るので、実行した曜日で入る日が変わってしまう。
# 2026-09-07 10:00 UTC = 月曜 19:00 JST。先週は 08/31(月)〜09/06(日)。
REEL_NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


def _reel_db(tmp_path, days: int, reaches=None):
    """先週の日ごとに1本ずつ、公開済みの投稿を並べたDBを作る。

    offset 1 が 09/06（日）で、offset 7 が 08/31（月）。days=7 でちょうど
    先週1週間ぶんになる。
    """
    from io import BytesIO

    from freming.db.connection import connect
    from freming.db.migrate import migrate
    from freming.db.repository import create_post, finish_post, record_reach

    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "reel.db"
    migrate(cfg.app.db_path)
    conn = connect(cfg.app.db_path)

    def jpeg(shade: int) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (1600, 1200), (shade, 90, 140)).save(buffer, "JPEG")
        return buffer.getvalue()

    now = REEL_NOW
    made = []
    for offset in range(1, days + 1):
        cursor = conn.execute(
            "INSERT INTO properties (source, source_url, title, summary, score, "
            "status, collected_at, display_name, caption_body) "
            "VALUES ('dezeen', ?, ?, 's', 80, 'delivered', "
            "'2026-08-01T00:00:00+00:00', ?, '本文') RETURNING id",
            (f"manual:r{offset}", f"House {offset}", f"House {offset}"),
        )
        property_id = cursor.fetchone()["id"]
        conn.execute(
            "INSERT INTO property_media (property_id, position, mime, content, "
            "created_at) VALUES (?, 1, 'image/jpeg', ?, ?)",
            (property_id, jpeg(30 * offset), now.isoformat()),
        )
        conn.execute(
            "INSERT INTO images (property_id, source_url, position, fetched_at) "
            "VALUES (?, 'upload:1', 1, ?)",
            (property_id, now.isoformat()),
        )
        conn.commit()
        at = (now - timedelta(days=offset)).isoformat()
        post_id = create_post(conn, "feed", at, property_id=property_id, caption="c")
        finish_post(conn, post_id, f"m{offset}", f"c{offset}")
        conn.execute("UPDATE posts SET published_at = ? WHERE id = ?", (at, post_id))
        record_reach(conn, post_id, (reaches or {}).get(offset, 100 * offset))
        made.append(post_id)
    conn.commit()
    return cfg, conn, made


@needs_ffmpeg
def test_週次リールは日ごとに1枚ずつ古い順に並ぶ(tmp_path):
    """日をまたいでリーチを比べない。同じ日の中でだけ1位を選ぶ。"""
    from freming.instagram.worker import build_weekly_reel

    cfg, conn, made = _reel_db(tmp_path, days=4)
    built = build_weekly_reel(cfg, conn, "token", tmp_path / "reel.mp4", now=REEL_NOW)
    conn.close()

    assert built.video.exists() and built.video.stat().st_size > 0
    assert built.result.image_count == 4
    # made は「1日前, 2日前, …」の順に作ってあるので、古い順は逆順
    assert [r["id"] for r in built.winners] == list(reversed(made))


@needs_ffmpeg
def test_同じ日なら高いほうを採る(tmp_path):
    from freming.db.repository import create_post, finish_post, record_reach
    from freming.instagram.worker import build_weekly_reel

    cfg, conn, made = _reel_db(tmp_path, days=2)
    # 1日前の枠にもう1本、リーチの高いものを足す
    now = REEL_NOW
    at = (now - timedelta(days=1)).isoformat()
    cursor = conn.execute(
        "INSERT INTO properties (source, source_url, title, summary, score, status, "
        "collected_at, display_name, caption_body) "
        "VALUES ('dezeen', 'manual:top', 'Top House', 's', 90, 'delivered', "
        "'2026-08-01T00:00:00+00:00', 'Top House', '本文') RETURNING id"
    )
    top_property = cursor.fetchone()["id"]
    conn.execute(
        "INSERT INTO property_media (property_id, position, mime, content, created_at) "
        "SELECT ?, 1, mime, content, created_at FROM property_media LIMIT 1",
        (top_property,),
    )
    conn.execute(
        "INSERT INTO images (property_id, source_url, position, fetched_at) "
        "VALUES (?, 'upload:1', 1, ?)", (top_property, now.isoformat()),
    )
    conn.commit()
    top = create_post(conn, "feed", at, property_id=top_property, caption="c")
    finish_post(conn, top, "mtop", "ctop")
    conn.execute("UPDATE posts SET published_at = ? WHERE id = ?", (at, top))
    record_reach(conn, top, 9999)
    conn.commit()

    built = build_weekly_reel(cfg, conn, "token", tmp_path / "reel.mp4", now=REEL_NOW)
    conn.close()
    assert top in [r["id"] for r in built.winners]
    assert made[0] not in [r["id"] for r in built.winners]


def test_1件しか無ければ組まない(tmp_path):
    """**1枚のリールは出さない。** 先週の投稿が揃ってから作り直す。"""
    from freming.instagram.worker import PostingError, build_weekly_reel

    cfg, conn, _ = _reel_db(tmp_path, days=1)
    with pytest.raises(PostingError, match="1 件しかありません"):
        build_weekly_reel(cfg, conn, "token", tmp_path / "reel.mp4", now=REEL_NOW)
    conn.close()


@needs_ffmpeg
def test_本文にクレジットが要る週は必ず入る(tmp_path):
    """CC BY は表記が要件。落とすとライセンス違反になる。"""
    from freming.instagram.worker import build_weekly_reel

    cfg, conn, _ = _reel_db(tmp_path, days=3)
    built = build_weekly_reel(cfg, conn, "token", tmp_path / "reel.mp4", now=REEL_NOW)
    conn.close()
    line = built.track.caption_line()
    if line:
        assert line in built.caption


# ----------------------------------------------------------------------
# 選抜の窓（2026-09-01）
#
# 「直近8日」の移動窓をやめ、暦の週（先週の月〜日）で切る。
# GitHub の定期実行は実測で3〜10時間ずれるので、移動窓だと同じ月曜の枠でも
# 走った時刻で入る日が変わる。
# ----------------------------------------------------------------------
def test_選抜の窓は先週の月曜から日曜():
    from freming.instagram.worker import last_week

    start, end = last_week(CONFIG, REEL_NOW)  # 月曜 19:00 JST に走った場合
    zone = ZoneInfo(CONFIG.instagram.timezone)
    assert start.astimezone(zone).isoformat() == "2026-08-31T00:00:00+09:00"
    assert end.astimezone(zone).isoformat() == "2026-09-07T00:00:00+09:00"
    assert start.astimezone(zone).weekday() == 0  # 月曜はじまり


def test_定期実行が遅れて翌日になっても同じ週を指す():
    """**GitHub は3〜10時間遅れる。** 月曜19:00 の枠が火曜未明に走っても、
    指す週が変わってはいけない（変わると先週の月曜が丸ごと抜ける）。"""
    from freming.instagram.worker import last_week

    on_time = last_week(CONFIG, REEL_NOW)
    late = last_week(CONFIG, REEL_NOW + timedelta(hours=10))  # 火曜 05:00 JST
    assert late == on_time


def _extra_post(conn, published_at: str, reach: int) -> int:
    """既にあるDBに、公開済みの通常投稿を1本足す。

    **物件は必ず新しく作る。** create_post は (property_id, kind) が
    衝突すると None を返すので、既存の物件を使い回すと1本も足されず、
    テストが素通りする（実際にそれで空振りした）。
    """
    from freming.db.repository import create_post, finish_post, record_reach

    cursor = conn.execute(
        "INSERT INTO properties (source, source_url, title, summary, score, "
        "status, collected_at, display_name, caption_body) "
        "VALUES ('dezeen', ?, ?, 's', 80, 'delivered', "
        "'2026-08-01T00:00:00+00:00', ?, '本文') RETURNING id",
        (f"manual:x{published_at}", f"Extra {published_at}", f"Extra {published_at}"),
    )
    property_id = cursor.fetchone()["id"]
    conn.execute(
        "INSERT INTO property_media (property_id, position, mime, content, created_at) "
        "SELECT ?, 1, mime, content, created_at FROM property_media LIMIT 1",
        (property_id,),
    )
    conn.execute(
        "INSERT INTO images (property_id, source_url, position, fetched_at) "
        "VALUES (?, 'upload:1', 1, ?)", (property_id, published_at),
    )
    conn.commit()
    post_id = create_post(conn, "feed", published_at, property_id=property_id, caption="c")
    assert post_id is not None, "投稿が作られていない。テストが空振りする"
    finish_post(conn, post_id, f"m{post_id}", f"c{post_id}")
    conn.execute(
        "UPDATE posts SET published_at = ? WHERE id = ?", (published_at, post_id)
    )
    record_reach(conn, post_id, reach)
    conn.commit()
    return post_id


def test_先週より前の投稿は入らない(tmp_path):
    """先々週の日曜（08/30）が混ざらないこと。1日ずれるだけで別の週になる。"""
    from freming.instagram.worker import daily_winners

    cfg, conn, made = _reel_db(tmp_path, days=7)  # 08/31(月)〜09/06(日)
    # リーチは最高。**それでも先週の外なので入らない。**
    old = _extra_post(conn, "2026-08-30T10:00:00+00:00", 99999)

    winners = daily_winners(cfg, conn, "token", REEL_NOW)
    conn.close()
    assert old not in [row["id"] for row in winners]
    assert [row["id"] for row in winners] == list(reversed(made))


def test_遅れて走っても今週の投稿を巻き込まない(tmp_path):
    """**これが移動窓の実害。**

    月曜19:00 の枠が遅れて火曜未明に走ると、移動窓は今週の月曜朝に出た
    通常投稿まで拾い、そのぶん先週の月曜が押し出されて落ちる。
    「先週のまとめ」に今週の1本が混ざり、先週の1日が消える。
    """
    from freming.instagram.worker import daily_winners

    cfg, conn, made = _reel_db(tmp_path, days=7)  # 08/31(月)〜09/06(日)
    # 今週の月曜 09:00 JST に出た通常投稿（リールより前に出ている）
    today = _extra_post(conn, "2026-09-07T00:00:00+00:00", 50000)

    late = REEL_NOW + timedelta(hours=10)  # 火曜 05:00 JST に起動
    winners = daily_winners(cfg, conn, "token", late)
    conn.close()

    ids = [row["id"] for row in winners]
    assert today not in ids, "今週の投稿が先週のまとめに混ざった"
    assert made[-1] in ids, "先週の月曜が押し出されて落ちた"
    assert ids == list(reversed(made))


def test_本文の件数は実際に入った数になる():
    """先週が6日ぶんしか無い週に「7軒」と書かない。"""
    from freming.instagram.caption import build_reel_caption

    caption = build_reel_caption(
        6, CONFIG.caption, names=[f"House {i}" for i in range(1, 7)],
        picked_by="recent",
    )
    assert "先週ご紹介した6軒" in caption
    assert "7軒" not in caption
    assert "{count}" not in caption
