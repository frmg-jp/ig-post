"""[6] Drive への納品。

    承認済み → 画像取得 → 正方形加工 → frmg_igNNN フォルダに納品

再実行しても重複納品しないことを二重に担保する。

  1. deliveries テーブルの property_id が UNIQUE
  2. 納品前に deliveries の存在を確認し、あればスキップ

単体実行:
    python -m freming.delivery.deliver [--limit 5] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

from freming.config import Config, load_config
from freming.db.connection import DbConnection, Row, connect
from freming.delivery.drive import DriveClient, DriveError, build_client
from freming.images.fetch import NoImagesFound, fetch_images
from freming.images.process import process_property_images
from freming.logging_setup import get_logger, setup_logging
from freming.net.client import HttpClient

log = get_logger(__name__)


@dataclass
class DeliveryResult:
    property_id: int
    folder_name: str
    image_count: int
    drive_folder_id: str


@dataclass
class DeliverStats:
    delivered: list[DeliveryResult] = field(default_factory=list)
    skipped_existing: int = 0
    no_images: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"納品 {len(self.delivered)} 件"
            f"（納品済みのためスキップ {self.skipped_existing} / "
            f"画像なし {self.no_images} / 失敗 {self.failed}）"
        )

    def report(self) -> str:
        return "\n".join(
            f"  {d.folder_name}  画像 {d.image_count} 枚  property_id={d.property_id}"
            for d in self.delivered
        )


def next_folder_name(conn: DbConnection, config: Config) -> str:
    """frmg_ig001 形式の次の連番。

    連番は deliveries の件数ではなく最大値から採る。途中の納品を
    削除しても番号が再利用されないようにするため。
    """
    rows = conn.execute("SELECT folder_name FROM deliveries").fetchall()
    last = 0
    for row in rows:
        digits = "".join(c for c in row["folder_name"] if c.isdigit())
        if digits:
            last = max(last, int(digits))
    return f"{config.drive.folder_prefix}{last + 1:0{config.drive.sequence_digits}d}"


def build_meta(row: Row, image_count: int, series_label: str | None = None) -> str:
    """納品フォルダに同梱するテキスト。投稿時の下書きに使う。

    出典URLは必ず入れる。あとから素材の出どころを辿れないと使えない。
    series は審査UIで人が付けた連載企画のラベル。
    """
    lines = [
        f"title: {row['title'] or ''}",
        f"summary: {row['summary'] or ''}",
        f"series: {series_label or ''}",
        f"genre: {row['genre'] or ''}",
        f"location: {', '.join(x for x in (row['location_city'], row['location_country']) if x)}",
        f"architect: {row['architect'] or ''}",
        f"year_built: {row['year_built'] or ''}",
        f"price: {row['price'] or ''}",
        f"score: {row['score'] if row['score'] is not None else ''}",
        f"source: {row['source']}",
        f"source_url: {row['source_url']}",
        f"images: {image_count}",
    ]
    if row["score_reason"]:
        lines.append(f"score_reason: {row['score_reason']}")
    return "\n".join(lines) + "\n"


def already_delivered(conn: DbConnection, property_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM deliveries WHERE property_id = ? LIMIT 1", (property_id,)
        ).fetchone()
        is not None
    )


def deliver_property(
    config: Config,
    conn: DbConnection,
    row: Row,
    drive: DriveClient,
    http: HttpClient,
    dry_run: bool = False,
) -> DeliveryResult | None:
    """1物件を納品する。既に納品済みなら None を返して何もしない。"""
    property_id = int(row["id"])
    if already_delivered(conn, property_id):
        log.info("納品済みのためスキップ: property_id=%s", property_id)
        return None

    fetch_images(config, conn, row, client=http)
    processed = process_property_images(config, conn, property_id)
    if not processed.outputs:
        raise NoImagesFound(f"加工できた画像がありません: property_id={property_id}")

    folder_name = next_folder_name(conn, config)
    if dry_run:
        log.info(
            "[dry-run] %s に %d 枚を納品します（Driveには書き込みません）",
            folder_name, len(processed.outputs),
        )
        return DeliveryResult(property_id, folder_name, len(processed.outputs), "")

    folder_id = drive.create_folder(folder_name, config.drive.parent_folder_id)
    for path in processed.outputs:
        drive.upload_file(path, path.name, folder_id, mime_type="image/jpeg")
    drive.upload_bytes(
        build_meta(
            row, len(processed.outputs), config.series_label(row["series"])
        ).encode("utf-8"),
        config.drive.meta_filename,
        folder_id,
        mime_type="text/plain",
    )

    # 納品記録は最後に書く。途中で落ちた場合は未納品として扱い、
    # 次回やり直せるようにする（重複より取りこぼしを検知しやすい）。
    conn.execute(
        "INSERT INTO deliveries (property_id, folder_name, image_count, drive_folder_id, "
        "delivered_at) VALUES (?, ?, ?, ?, ?)",
        (
            property_id, folder_name, len(processed.outputs), folder_id,
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.execute("UPDATE properties SET status = 'delivered' WHERE id = ?", (property_id,))
    conn.commit()
    log.info("納品しました: %s（%d枚）", folder_name, len(processed.outputs))
    return DeliveryResult(property_id, folder_name, len(processed.outputs), folder_id)


def deliver_approved(
    config: Config,
    conn: DbConnection,
    limit: int | None = None,
    dry_run: bool = False,
    drive: DriveClient | None = None,
    http: HttpClient | None = None,
) -> DeliverStats:
    """承認済みで未納品の物件をまとめて納品する。"""
    stats = DeliverStats()
    rows = conn.execute(
        "SELECT * FROM properties WHERE status = 'approved' "
        "ORDER BY score DESC, id LIMIT ?",
        (limit or 20,),
    ).fetchall()
    if not rows:
        log.info("納品対象がありません（承認済みで未納品の候補なし）")
        return stats

    owns_http = http is None
    # dry-run では Drive の認証すら行わない。認証を通さずに手前の工程
    # （画像取得・加工）だけを確かめられるようにするため。
    if drive is None and not dry_run:
        drive = build_client(config.drive)
    http = http or HttpClient(config.http)
    try:
        for row in rows:
            try:
                result = deliver_property(config, conn, row, drive, http, dry_run)
            except NoImagesFound as exc:
                log.error("画像が用意できませんでした: property_id=%s (%s)", row["id"], exc)
                stats.no_images += 1
                continue
            except DriveError as exc:
                log.error("Driveへの納品に失敗: property_id=%s (%s)", row["id"], exc)
                stats.failed += 1
                continue
            if result is None:
                stats.skipped_existing += 1
            else:
                stats.delivered.append(result)
    finally:
        if owns_http:
            http.close()

    log.info(stats.summary())
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="承認済み物件をDriveへ納品する")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Driveに書き込まず手前まで実行")
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config.app.log_dir, config.app.log_level)
    conn = connect(config.app.target())
    try:
        stats = deliver_approved(config, conn, args.limit, args.dry_run)
    finally:
        conn.close()
    print(stats.summary())
    print(stats.report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
