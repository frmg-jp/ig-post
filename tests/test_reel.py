"""[9] 週次リールの検証。

ffmpeg が要るのは動画を書き出すところだけ。コマの組み立てと音源の
割り当ては ffmpeg 無しで検証できるので、CI でも必ず通る。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

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

def _reel_db(tmp_path, days: int, reaches=None):
    """日ごとに1本ずつ公開済みの投稿を並べたDBを作る。"""
    from datetime import UTC, datetime, timedelta
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

    now = datetime.now(UTC)
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
    built = build_weekly_reel(cfg, conn, "token", tmp_path / "reel.mp4")
    conn.close()

    assert built.video.exists() and built.video.stat().st_size > 0
    assert built.result.image_count == 4
    # made は「1日前, 2日前, …」の順に作ってあるので、古い順は逆順
    assert [r["id"] for r in built.winners] == list(reversed(made))


@needs_ffmpeg
def test_同じ日なら高いほうを採る(tmp_path):
    from datetime import UTC, datetime, timedelta

    from freming.db.repository import create_post, finish_post, record_reach
    from freming.instagram.worker import build_weekly_reel

    cfg, conn, made = _reel_db(tmp_path, days=2)
    # 1日前の枠にもう1本、リーチの高いものを足す
    now = datetime.now(UTC)
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

    built = build_weekly_reel(cfg, conn, "token", tmp_path / "reel.mp4")
    conn.close()
    assert top in [r["id"] for r in built.winners]
    assert made[0] not in [r["id"] for r in built.winners]


def test_1件しか無ければ組まない(tmp_path):
    """**1枚のリールは出さない。** 先週の投稿が揃ってから作り直す。"""
    from freming.instagram.worker import PostingError, build_weekly_reel

    cfg, conn, _ = _reel_db(tmp_path, days=1)
    with pytest.raises(PostingError, match="1 件しかありません"):
        build_weekly_reel(cfg, conn, "token", tmp_path / "reel.mp4")
    conn.close()


@needs_ffmpeg
def test_本文にクレジットが要る週は必ず入る(tmp_path):
    """CC BY は表記が要件。落とすとライセンス違反になる。"""
    from freming.instagram.worker import build_weekly_reel

    cfg, conn, _ = _reel_db(tmp_path, days=3)
    built = build_weekly_reel(cfg, conn, "token", tmp_path / "reel.mp4")
    conn.close()
    line = built.track.caption_line()
    if line:
        assert line in built.caption
