"""[9] 正方形の納品画像から 1080×1920 のリールを組む。

    正方形を9:16に組む → 並べてクロスフェード → 音を焼き込む

決まっていること（2026-08-07 に実物を見て確定）:

  - 上下は**同じ写真をぼかして暗くしたもの**で埋める。白帯や黒帯にすると
    そこだけ別のアプリに見える。元写真の色がそのまま背景に回るので、
    1枚ごとに地の色が変わっても不自然にならない。
  - 正方形は中央より **60px 上**に置く。Reels は下側にキャプションと
    ボタンが重なるので、真ん中に置くと下端がUIに近すぎる。
  - 1枚ごとに **1.10倍までゆっくり寄る**。静止のまま繋いだ版も作って
    見比べたうえで、動くほうを採った。
  - **文字は載せない。** 週数もロゴも入れない判断。

音源は `assets/audio/` に置いた5曲を週ごとに回す。CC BY の曲は
キャプションにクレジットが要るので、`audio_for_week()` が
表記の要否と本文を一緒に返す。**表記を落とすとライセンス違反になる。**

ffmpeg が要る。無い環境では ReelError で落とす（黙って音無しの動画を
作ったりはしない）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from freming.config import ReelConfig
from freming.logging_setup import get_logger

log = get_logger(__name__)

MANIFEST = Path("assets/audio/manifest.json")


class ReelError(RuntimeError):
    """リールを組めなかった。原因を必ずメッセージに入れる。"""


@dataclass
class Track:
    """焼き込む音源1曲。"""

    path: Path
    title: str
    artist: str
    license: str
    attribution_required: bool
    credit: str | None

    def caption_line(self) -> str:
        """キャプションの末尾に足す1行。表記不要なら空文字。"""
        return self.credit or "" if self.attribution_required else ""


@dataclass
class ReelResult:
    path: Path
    seconds: float
    per_image_sec: float
    image_count: int
    track: Track


# ----------------------------------------------------------------------
# 1枚を 9:16 のコマにする
# ----------------------------------------------------------------------
def compose_frame(square: Path, dest: Path, config: ReelConfig) -> None:
    """正方形1枚を 1080×1920 のコマにして保存する。

    背景は同じ写真。縦を埋めるまで拡大してから中央で切り、ぼかして
    暗くする。前景はもとの正方形をそのまま重ねるので、写真自体は
    一切トリミングされない（納品画像＝投稿画像であることを崩さない）。
    """
    from PIL import Image, ImageEnhance, ImageFilter

    width, height = config.size
    top = (height - width) // 2 - config.square_offset_px

    with Image.open(square) as img:
        img = img.convert("RGB")
        scale = max(width / img.width, height / img.height)
        bg = img.resize(
            (round(img.width * scale), round(img.height * scale)),
            Image.Resampling.LANCZOS,
        )
        left = (bg.width - width) // 2
        upper = (bg.height - height) // 2
        bg = bg.crop((left, upper, left + width, upper + height))
        bg = bg.filter(ImageFilter.GaussianBlur(config.blur_radius))
        bg = ImageEnhance.Brightness(bg).enhance(config.bg_brightness)
        bg.paste(img.resize((width, width), Image.Resampling.LANCZOS), (0, top))

        dest.parent.mkdir(parents=True, exist_ok=True)
        bg.save(dest, "JPEG", quality=config.jpeg_quality, optimize=True)


# ----------------------------------------------------------------------
# 音源
# ----------------------------------------------------------------------
def load_tracks(manifest: Path | None = None) -> list[Track]:
    """manifest.json から音源の一覧を order 順に読む。"""
    path = manifest or MANIFEST
    if not path.exists():
        raise ReelError(
            f"音源の一覧が見つかりません: {path}\n"
            "assets/audio/manifest.json を用意してください。"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    tracks = []
    for row in sorted(raw["tracks"], key=lambda r: r["order"]):
        # manifest の file はリポジトリ相対だが、音源は manifest と同じ
        # ディレクトリに置く。カレントディレクトリに依存させない。
        file = path.parent / Path(row["file"]).name
        tracks.append(
            Track(
                path=file,
                title=row["title"],
                artist=row["artist"],
                license=row["license"],
                attribution_required=row["attribution_required"],
                credit=row.get("credit"),
            )
        )
    return tracks


def audio_for_week(week: int, manifest: Path | None = None) -> Track:
    """その週に使う1曲。

    週番号の剰余で選ぶ。同じ週に2回走っても同じ曲になるので、
    作り直しても音が変わらない。
    """
    tracks = load_tracks(manifest)
    if not tracks:
        raise ReelError("音源が1曲もありません。")
    return tracks[week % len(tracks)]


# ----------------------------------------------------------------------
# 動画に組む
# ----------------------------------------------------------------------
def _require_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise ReelError(
            "ffmpeg が見つかりません。リールの生成には ffmpeg が要ります。\n"
            "  macOS: brew install ffmpeg\n"
            "  Ubuntu: apt-get install ffmpeg"
        )
    return found


def _filters(count: int, per_image: float, config: ReelConfig) -> list[str]:
    """filter_complex の中身を組み立てる。"""
    width, height = config.size
    frames = int(per_image * config.fps)
    steps: list[str] = []

    for i in range(count):
        if config.zoom > 0:
            # そのまま zoompan で寄るとギザつくので、いったん倍のサイズに
            # してから 1080×1920 に落とす。元画像を引き伸ばしてはいない。
            steps.append(
                f"[{i}:v]scale={width * 2}:{height * 2}:flags=lanczos,"
                f"zoompan=z='1+{config.zoom}*on/{frames - 1}':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={frames}:s={width}x{height}:fps={config.fps},setsar=1[v{i}]"
            )
        else:
            steps.append(f"[{i}:v]fps={config.fps},setsar=1[v{i}]")

    last = "v0"
    for k in range(count - 1):
        # xfade を数珠つなぎにする。k番目の繋ぎ目は (k+1)*(尺-繋ぎ) 秒から。
        offset = (k + 1) * (per_image - config.crossfade_sec)
        steps.append(
            f"[{last}][v{k + 1}]xfade=transition=fade:"
            f"duration={config.crossfade_sec}:offset={offset:.4f}[x{k}]"
        )
        last = f"x{k}"

    steps.append(f"[{last}]trim=duration={config.total_sec},format=yuv420p[vout]")
    steps.append(
        f"[{count}:a]atrim=duration={config.total_sec},"
        f"afade=t=in:st=0:d={config.audio_fade_in_sec},"
        f"afade=t=out:st={config.total_sec - config.audio_fade_out_sec}:"
        f"d={config.audio_fade_out_sec}[aout]"
    )
    return steps


def build_reel(
    squares: list[Path],
    track: Track,
    dest: Path,
    config: ReelConfig,
    work_dir: Path | None = None,
) -> ReelResult:
    """正方形の並びと音源からリールを1本作る。

    squares は投稿順（1枚目が先頭）。枚数は config.image_count と
    違っていてもよく、全体の尺は必ず config.total_sec に収まる。
    """
    if not squares:
        raise ReelError("画像が1枚もありません。")
    if not track.path.exists():
        raise ReelError(f"音源が見つかりません: {track.path}")
    ffmpeg = _require_ffmpeg()

    count = len(squares)
    # 繋ぎ目のぶんだけ1枚を長くしないと、全体が指定の尺より短くなる。
    per_image = (config.total_sec + (count - 1) * config.crossfade_sec) / count

    work = work_dir or dest.parent / f".{dest.stem}-frames"
    work.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, square in enumerate(squares, start=1):
        frame = work / f"{index:02d}.jpg"
        compose_frame(square, frame, config)
        frames.append(frame)

    args: list[str] = [ffmpeg, "-v", "error", "-y"]
    for frame in frames:
        args += [
            "-loop", "1",
            "-framerate", str(config.fps),
            "-t", f"{per_image:.4f}",
            "-i", str(frame),
        ]
    args += ["-i", str(track.path)]
    args += [
        "-filter_complex", ";".join(_filters(count, per_image, config)),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", config.x264_preset, "-crf", str(config.crf),
        "-pix_fmt", "yuv420p", "-r", str(config.fps),
        "-c:a", "aac", "-b:a", config.audio_bitrate, "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart", "-shortest",
        str(dest),
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(args, capture_output=True)
    if done.returncode != 0:
        raise ReelError(f"ffmpeg が失敗しました:\n{done.stderr.decode()[-1500:]}")

    log.info(
        "リールを作りました: %s（%d枚 / %.1f秒 / 音源 %s）",
        dest, count, config.total_sec, track.title,
    )
    return ReelResult(
        path=dest,
        seconds=config.total_sec,
        per_image_sec=per_image,
        image_count=count,
        track=track,
    )


__all__ = [
    "ReelError",
    "ReelResult",
    "Track",
    "audio_for_week",
    "build_reel",
    "compose_frame",
    "load_tracks",
]
