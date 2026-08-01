"""[5] 1080×1080 への加工。

建築写真は構図が意味を持つので、既定は中央クロップ。ただし極端な
横長・縦長（パノラマや縦位置の全景）を中央クロップすると建物が
切れるため、`process.pad_when_aspect_over` を超える比率のものは
クロップせず余白で埋める。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from freming.config import Config, ProcessConfig
from freming.logging_setup import get_logger

log = get_logger(__name__)

_RESAMPLE = {"lanczos": "LANCZOS", "bicubic": "BICUBIC", "bilinear": "BILINEAR"}


@dataclass
class ProcessStats:
    property_id: int
    processed: int = 0
    padded: int = 0
    cropped: int = 0
    failed: int = 0
    outputs: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"[加工] property_id={self.property_id} {self.processed} 枚"
            f"（中央クロップ {self.cropped} / 余白 {self.padded} / 失敗 {self.failed}）"
        )


def to_square(source: Path, dest: Path, config: ProcessConfig) -> str:
    """1枚を正方形に加工して保存する。戻り値は 'crop' か 'pad'。"""
    from PIL import Image, ImageOps

    width, height = config.output_size
    resample = getattr(Image.Resampling, _RESAMPLE.get(config.resample, "LANCZOS"))

    with Image.open(source) as img:
        # Exif の向き情報を反映してから加工する。無視すると横倒しのまま出る。
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")

        long_side, short_side = max(img.size), min(img.size)
        aspect = long_side / short_side if short_side else 1.0

        if aspect > config.pad_when_aspect_over:
            # 建物が切れるのでクロップしない。全体を収めて余白で埋める。
            fitted = ImageOps.contain(img, (width, height), resample)
            canvas = Image.new("RGB", (width, height), config.pad_color)
            canvas.paste(
                fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2)
            )
            result, mode = canvas, "pad"
        else:
            result, mode = ImageOps.fit(img, (width, height), resample, centering=(0.5, 0.5)), "crop"

        dest.parent.mkdir(parents=True, exist_ok=True)
        result.save(dest, "JPEG", quality=config.jpeg_quality, optimize=True)
    return mode


def process_property_images(
    config: Config, conn: sqlite3.Connection, property_id: int
) -> ProcessStats:
    """1物件分の画像を position 順に 01.jpg … として書き出す。"""
    stats = ProcessStats(property_id=property_id)
    rows = conn.execute(
        "SELECT * FROM images WHERE property_id = ? ORDER BY position LIMIT ?",
        (property_id, config.images.max_per_property),
    ).fetchall()

    out_dir = Path(config.images.work_dir) / f"p{property_id:06d}" / "out"
    for index, row in enumerate(rows, start=1):
        source = Path(row["local_path"])
        if not source.exists():
            log.warning("元画像が見つかりません: %s", source)
            stats.failed += 1
            continue
        dest = out_dir / f"{index:02d}.jpg"
        try:
            mode = to_square(source, dest, config.process)
        except Exception as exc:  # noqa: BLE001 - 1枚の失敗で残りを止めない
            log.warning("加工に失敗しました: %s (%s)", source, exc)
            stats.failed += 1
            continue
        conn.execute(
            "UPDATE images SET output_path = ? WHERE id = ?", (str(dest), row["id"])
        )
        stats.processed += 1
        stats.outputs.append(dest)
        if mode == "pad":
            stats.padded += 1
        else:
            stats.cropped += 1

    conn.commit()
    log.info(stats.summary())
    return stats
