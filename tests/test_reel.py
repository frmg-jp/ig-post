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


def test_正方形は設定どおりの高さに置かれる(tmp_path, square):
    """square_offset_px のぶんだけ中央から上げる。**既定は 0＝中央。**

    以前は 60px 上げていた（Reels の下部UIを避ける名目）。1080 の正方形を
    1920 に置くと中央でも下に420px残るのでUIには掛からず、投稿ページや
    プロフィールで下だけ間延びして見えるだけだった（2026-09-01 に実物で
    指摘を受けて中央に戻した）。

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
# 週次リールの選定と組み立て
#
# 材料は**アカウントに実際に出ている投稿**（/me/media）。予定表からは
# 組まない。予定表だけを見ると、手で出した投稿が丸ごと抜けるうえ、写真も
# 「物件の1枚目」になり、審査UIで並べ替えた投稿とは別のカットになる。
# 2026-09-01 の初回で両方が起きた（7軒あるところを6軒で出し、表紙も
# 投稿と違うものが並んだ）。
#
# 出す処理と試写が**同じ関数**を通ることも要点。別々に書くと、試写で
# 見たものと出るものが食い違う。食い違う試写は無いより悪い。
# ----------------------------------------------------------------------

# 週次リールが走る想定の時刻。**壁時計を使わない。** 選抜は暦の週
# （先週の月〜日）で切るので、実行した曜日で入る日が変わってしまう。
# 2026-09-07 10:00 UTC = 月曜 19:00 JST。先週は 08/31(月)〜09/06(日)。
REEL_NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


def _item(media_id: str, when: str, caption: str = "・\nHouse", children: int = 10):
    """/me/media が返す1件。when は UTC の 'YYYY-MM-DD HH:MM'。"""
    from freming.instagram.mymedia import MediaItem

    return MediaItem(
        id=media_id,
        media_type="CAROUSEL_ALBUM" if children else "IMAGE",
        permalink=f"https://instagram.test/{media_id}",
        timestamp=when.replace(" ", "T") + ":00+0000",
        caption=caption,
        image_url=f"https://cdn.test/{media_id}.jpg",
        child_count=children,
    )


def _fake_account(monkeypatch, items, reach=None, shades=None):
    """アカウント側（/me/media・リーチ・画像取得）を差し替える。

    **Meta へは一度も出ない。** 画像は media_id ごとに色を変えて作るので、
    どのコマがどの投稿から来たかを色で追える。
    """
    from freming.instagram import mymedia
    from freming.instagram import worker as worker_mod

    monkeypatch.setattr(mymedia, "recent_media", lambda *a, **k: list(items))
    monkeypatch.setattr(worker_mod, "account_id", lambda token: "ig-1")

    if reach is None:
        def _no_scope(token, media_id):
            from freming.instagram.insights import MissingInsightsScope

            raise MissingInsightsScope("権限なし")
        monkeypatch.setattr(worker_mod, "media_reach", _no_scope)
    else:
        monkeypatch.setattr(worker_mod, "media_reach", lambda t, mid: reach.get(mid, 0))

    def fake_download(url, dest):
        shade = (shades or {}).get(url.rsplit("/", 1)[-1][:-4], 120)
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1600, 1200), (shade, 90, 140)).save(dest, "JPEG")
        return dest

    monkeypatch.setattr(mymedia, "download_image", fake_download)


def _cfg(tmp_path):
    from freming.db.connection import connect
    from freming.db.migrate import migrate

    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "reel.db"
    migrate(cfg.app.db_path)
    return cfg, connect(cfg.app.db_path)


# 先週（08/31 月〜09/06 日）に1日1本ずつ。UTC 00:04 = JST 09:04。
WEEK = [
    _item("m6", "2026-09-06 00:04"),
    _item("m5", "2026-09-05 00:04"),
    _item("m4", "2026-09-04 00:04"),
    _item("m3", "2026-09-03 00:04"),
    _item("m2", "2026-09-02 00:04"),
    _item("m1", "2026-09-01 00:04"),
    _item("m0", "2026-08-31 00:04"),
]


def test_先週の投稿が古い順に並ぶ(tmp_path, monkeypatch):
    from freming.instagram.worker import weekly_picks

    _fake_account(monkeypatch, WEEK)
    cfg, conn = _cfg(tmp_path)
    picks, picked_by = weekly_picks(cfg, conn, "tok", "ig-1", REEL_NOW)
    conn.close()

    assert [p.media_id for p in picks] == ["m0", "m1", "m2", "m3", "m4", "m5", "m6"]
    assert picked_by == "recent"


def test_手で出した投稿も入る(tmp_path, monkeypatch):
    """**これが 2026-09-01 に落ちた分。** 予定表に無い投稿を拾えること。"""
    from freming.instagram.worker import weekly_picks

    # 08/27 相当の位置（先週の水曜 09/02）に、手で出した1本を足す
    manual = _item("manual-1", "2026-09-02 04:10", caption="・\n手で出した家")
    items = [i for i in WEEK if i.id != "m2"] + [manual]
    _fake_account(monkeypatch, items)
    cfg, conn = _cfg(tmp_path)
    picks, _ = weekly_picks(cfg, conn, "tok", "ig-1", REEL_NOW)
    conn.close()

    assert "manual-1" in [p.media_id for p in picks]
    assert len(picks) == 7, "手で出した日が抜けている"
    manual_pick = next(p for p in picks if p.media_id == "manual-1")
    assert manual_pick.name == "手で出した家"


def test_動画は材料にしない(tmp_path, monkeypatch):
    """リール自身が翌週の材料になると、入れ子になっていく。"""
    from freming.instagram.worker import weekly_picks

    reel = _item("reel-1", "2026-09-06 10:00", children=0)
    reel.media_type = "VIDEO"
    _fake_account(monkeypatch, [*WEEK, reel])
    cfg, conn = _cfg(tmp_path)
    picks, _ = weekly_picks(cfg, conn, "tok", "ig-1", REEL_NOW)
    conn.close()

    assert "reel-1" not in [p.media_id for p in picks]
    assert len(picks) == 7


def test_先週より前と今週は入らない(tmp_path, monkeypatch):
    """1日ずれるだけで別の週。前後どちらも混ぜない。"""
    from freming.instagram.worker import weekly_picks

    before = _item("before", "2026-08-30 00:04")   # 先々週の日曜
    after = _item("after", "2026-09-07 00:04")     # 今週の月曜
    _fake_account(monkeypatch, [after, *WEEK, before])
    cfg, conn = _cfg(tmp_path)
    picks, _ = weekly_picks(cfg, conn, "tok", "ig-1", REEL_NOW)
    conn.close()

    got = [p.media_id for p in picks]
    assert "before" not in got and "after" not in got
    assert got == ["m0", "m1", "m2", "m3", "m4", "m5", "m6"]


def test_遅れて走っても同じ週を指す(tmp_path, monkeypatch):
    """**GitHub は3〜10時間遅れる。** 火曜未明に走っても中身が変わらない。"""
    from freming.instagram.worker import weekly_picks

    after = _item("after", "2026-09-07 00:04")  # 今週の月曜朝の通常投稿
    _fake_account(monkeypatch, [after, *WEEK])
    cfg, conn = _cfg(tmp_path)
    late = REEL_NOW + timedelta(hours=10)
    picks, _ = weekly_picks(cfg, conn, "tok", "ig-1", late)
    conn.close()

    got = [p.media_id for p in picks]
    assert "after" not in got, "今週の投稿が混ざった"
    assert got == ["m0", "m1", "m2", "m3", "m4", "m5", "m6"]


def test_同じ日に2本ならリーチの高い方(tmp_path, monkeypatch):
    from freming.instagram.worker import weekly_picks

    extra = _item("m2b", "2026-09-02 10:00")
    reach = {i.id: 100 for i in WEEK}
    reach["m2b"] = 9999
    _fake_account(monkeypatch, [*WEEK, extra], reach=reach)
    cfg, conn = _cfg(tmp_path)
    picks, picked_by = weekly_picks(cfg, conn, "tok", "ig-1", REEL_NOW)
    conn.close()

    assert picked_by == "reach"
    assert len(picks) == 7, "1日1本になっていない"
    assert "m2b" in [p.media_id for p in picks]
    assert "m2" not in [p.media_id for p in picks]


def test_1日1本の週はいちばん見られたと名乗らない(tmp_path, monkeypatch):
    """選抜が起きていない週に「いちばん見られた」と書かない。"""
    from freming.instagram.worker import weekly_picks

    _fake_account(monkeypatch, WEEK, reach={i.id: 100 for i in WEEK})
    cfg, conn = _cfg(tmp_path)
    _, picked_by = weekly_picks(cfg, conn, "tok", "ig-1", REEL_NOW)
    conn.close()
    assert picked_by == "recent"


def test_リーチが読めなければ先に出た方(tmp_path, monkeypatch):
    from freming.instagram.worker import weekly_picks

    extra = _item("m2b", "2026-09-02 10:00")     # 同じ日の2本目（あと）
    _fake_account(monkeypatch, [*WEEK, extra])   # reach なし＝権限なし
    cfg, conn = _cfg(tmp_path)
    picks, picked_by = weekly_picks(
        cfg, conn, "tok", "ig-1", REEL_NOW, use_reach=False
    )
    conn.close()

    assert picked_by == "recent"
    assert "m2" in [p.media_id for p in picks], "先に出た方を採っていない"
    assert "m2b" not in [p.media_id for p in picks]


@needs_ffmpeg
def test_組んだ動画は投稿と同じ表紙になる(tmp_path, monkeypatch):
    """**リールのコマ＝投稿の表紙。** 別のカットにならないこと。

    以前は物件の「1枚目」を使っており、審査UIで並べ替えた投稿とは違う
    写真が並んだ。いまは /me/media が返す表紙をそのまま使う。
    """
    from freming.instagram.worker import build_weekly_reel

    shades = {f"m{i}": 20 + 30 * i for i in range(7)}
    _fake_account(monkeypatch, WEEK, shades=shades)
    cfg, conn = _cfg(tmp_path)
    built = build_weekly_reel(
        cfg, conn, "tok", tmp_path / "reel.mp4", now=REEL_NOW, ig_id="ig-1"
    )
    conn.close()

    assert built.result.image_count == 7
    frames = sorted((tmp_path / ".reel-frames").glob("src-*.jpg"))
    assert len(frames) == 7
    # 1枚目は m0（先週の月曜）の表紙。色で確かめる。
    with Image.open(frames[0]) as first:
        assert first.size == tuple(cfg.process.output_size)
        assert first.getpixel((540, 540))[0] == pytest.approx(shades["m0"], abs=12)


@needs_ffmpeg
def test_本文には入った件数と名前が並ぶ(tmp_path, monkeypatch):
    from freming.instagram.worker import build_weekly_reel

    items = [_item(i.id, i.timestamp[:16].replace("T", " "),
                   caption=f"・\nHouse {i.id}") for i in WEEK]
    _fake_account(monkeypatch, items)
    cfg, conn = _cfg(tmp_path)
    built = build_weekly_reel(
        cfg, conn, "tok", tmp_path / "reel.mp4", now=REEL_NOW, ig_id="ig-1"
    )
    conn.close()

    assert "先週ご紹介した7軒" in built.caption
    assert "いちばん見られた" not in built.caption
    for item in WEEK:
        assert f"House {item.id}" in built.caption


def test_2件に満たなければ組まない(tmp_path, monkeypatch):
    """**1枚のリールは出さない。**"""
    from freming.instagram.worker import PostingError, build_weekly_reel

    _fake_account(monkeypatch, WEEK[:1])
    cfg, conn = _cfg(tmp_path)
    with pytest.raises(PostingError, match="1 件しかありません"):
        build_weekly_reel(
            cfg, conn, "tok", tmp_path / "reel.mp4", now=REEL_NOW, ig_id="ig-1"
        )
    conn.close()


def test_選抜の窓は先週の月曜から日曜():
    from freming.instagram.worker import last_week

    start, end = last_week(CONFIG, REEL_NOW)
    zone = ZoneInfo(CONFIG.instagram.timezone)
    assert start.astimezone(zone).isoformat() == "2026-08-31T00:00:00+09:00"
    assert end.astimezone(zone).isoformat() == "2026-09-07T00:00:00+09:00"
    assert start.astimezone(zone).weekday() == 0


def test_定期実行が遅れて翌日になっても同じ週を指す():
    from freming.instagram.worker import last_week

    assert last_week(CONFIG, REEL_NOW + timedelta(hours=10)) == last_week(
        CONFIG, REEL_NOW
    )


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
