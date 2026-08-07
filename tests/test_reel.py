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
