"""Drive 疎通確認（プリフライト）。

「フォルダは作れるのに画像が入らない」という既知の不具合を、実際に書き込んで
確かめる。納品処理の起動時にも同じ関数を呼ぶことで、権限不足のまま処理を
走らせて空フォルダを量産する事故を防ぐ。

検査項目:
  1. サービスアカウント鍵の読み込み（メールアドレスを表示）
  2. サービスアカウントの保存容量
  3. 納品先フォルダの素性（共有ドライブか / 書き込み可能か）
  4. テキストファイルの作成 → サイズ検証 → 削除
  5. サブフォルダ作成 → 実画像アップロード → サイズ検証 → 後片付け
  6. 既存 frmg_ig* フォルダの中身の点検（空フォルダの検出）
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from freming.config import DriveConfig
from freming.delivery.drive import (
    DriveClient,
    DriveError,
    DriveQuotaError,
    FOLDER_MIME,
)
from freming.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    remedy: str = ""
    fatal: bool = True  # False の場合は警告扱い（全体の成否に影響しない）


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)
    account_email: str = ""

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.fatal)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        level = log.info if check.ok else (log.error if check.fatal else log.warning)
        level("[%s] %s — %s", "OK" if check.ok else ("NG" if check.fatal else "WARN"),
              check.name, check.detail)
        if not check.ok and check.remedy:
            level("  → 対処: %s", check.remedy)
        return check


def _make_test_jpeg() -> bytes:
    """検証用の 1080x1080 JPEG を生成する（実際の納品と同じ形式）。"""
    from PIL import Image

    img = Image.new("RGB", (1080, 1080), (240, 238, 234))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def run_preflight(
    config: DriveConfig, cleanup: bool = True, open_browser: bool = True
) -> PreflightReport:
    """Drive への書き込みを実際に試して結果を返す。"""
    report = PreflightReport()

    # 1. 認証 -----------------------------------------------------------
    try:
        client = DriveClient(config, open_browser=open_browser)
    except DriveError as exc:
        report.add(Check(f"認証（{config.auth_mode}）", False, str(exc),
                         "config.yaml の drive.auth_mode と認証情報の配置を確認してください"))
        return report

    report.account_email = client.account_hint
    auth_check = report.add(Check(f"認証（{config.auth_mode}）", True, "認証成功"))

    # 2. 保存容量 -------------------------------------------------------
    try:
        about = client.storage_quota()
        report.account_email = about.get("user", {}).get("emailAddress") or report.account_email
        auth_check.detail = f"認証成功: {report.account_email}"
        quota = about.get("storageQuota", {})
        limit = quota.get("limit")
        usage = quota.get("usage", "0")
        if limit is None:
            detail = f"使用量 {usage} バイト（上限の指定なし）"
        else:
            detail = f"使用量 {usage} / 上限 {limit} バイト"
        report.add(Check("保存容量の確認", True, detail, fatal=False))
    except DriveError as exc:
        report.add(Check("保存容量の確認", False, str(exc), fatal=False))

    # 3. 納品先フォルダ --------------------------------------------------
    try:
        folder = client.get_folder_info(config.parent_folder_id)
    except DriveError as exc:
        report.add(Check("納品先フォルダの取得", False, str(exc),
                         "フォルダIDが正しいか、認証したアカウントに共有されているか確認"))
        return report

    if folder.get("mimeType") != FOLDER_MIME:
        report.add(Check("納品先フォルダの取得", False,
                         f"指定IDはフォルダではありません: {folder.get('mimeType')}",
                         "drive.parent_folder_id にフォルダのIDを指定してください"))
        return report

    drive_id = folder.get("driveId")
    caps = folder.get("capabilities", {})
    report.add(Check(
        "納品先フォルダの取得", True,
        f"「{folder.get('name')}」 (id={folder['id']}) "
        f"{'共有ドライブ配下 driveId=' + drive_id if drive_id else 'マイドライブ配下'}",
    ))

    report.add(Check(
        "子要素の追加権限 (canAddChildren)",
        bool(caps.get("canAddChildren")),
        f"canAddChildren={caps.get('canAddChildren')}, canEdit={caps.get('canEdit')}",
        remedy=(f"{report.account_email} に編集権限を与えてください"
                "（共有ドライブならメンバーに追加して「コンテンツ管理者」以上）"),
    ))

    if drive_id:
        report.add(Check(
            "共有ドライブ配下かどうか", True,
            f"共有ドライブ配下です（driveId={drive_id}）。保存容量はドライブ側が持つため、"
            "サービスアカウントの容量制限を受けません。",
        ))
        if config.shared_drive_id != drive_id:
            report.add(Check(
                "config.yaml の shared_drive_id", False,
                f"設定値 {config.shared_drive_id!r} が実際の driveId と一致しません。",
                remedy=f'config.yaml の drive.shared_drive_id を "{drive_id}" に設定してください',
                fatal=False,
            ))
    elif config.auth_mode != "service_account":
        report.add(Check(
            "共有ドライブ配下かどうか", True,
            "マイドライブ配下ですが、人のアカウントとして認証しているため"
            "そのアカウントの保存容量が使われます（サービスアカウント特有の"
            "容量問題は発生しません）。",
            fatal=False,
        ))
    else:
        report.add(Check(
            "共有ドライブ配下かどうか", False,
            "納品先が個人のマイドライブ配下です。",
            remedy=(
                "サービスアカウントはマイドライブに保存容量を持たないため、"
                "フォルダ（0バイト）は作成できても画像アップロードが "
                "storageQuotaExceeded で失敗します。納品先を共有ドライブ"
                "（Shared Drive）に作り直し、そのドライブにサービスアカウントを"
                "コンテンツ管理者として追加したうえで、config.yaml の "
                "drive.parent_folder_id と drive.shared_drive_id を更新してください。"
            ),
            fatal=False,  # 実際の書き込みテスト（項目5）で最終判定する
        ))

    # 4. テキストファイルの作成・検証・削除 --------------------------------
    test_file_id: str | None = None
    try:
        uploaded = client.upload_bytes(
            data=b"freming write test\n",
            name=config.preflight.test_filename,
            parent_id=config.parent_folder_id,
            mime_type="text/plain",
        )
        test_file_id = uploaded.id
        report.add(Check("テキスト書き込みテスト", True,
                         f"{uploaded.name} を作成し {uploaded.size} バイトを確認"))
    except DriveQuotaError as exc:
        report.add(Check(
            "テキスト書き込みテスト", False, str(exc),
            remedy=("納品先を共有ドライブに変更してください。"
                    "これが現行システムの「フォルダはあるのに画像が入らない」原因です。"),
        ))
        return report
    except DriveError as exc:
        report.add(Check("テキスト書き込みテスト", False, str(exc),
                         remedy=f"{report.account_email} に編集権限を与えてください"))
        return report
    finally:
        if cleanup and test_file_id:
            _safe_delete(client, test_file_id, report)

    # 5. サブフォルダ + 実画像アップロード --------------------------------
    sub_folder_id: str | None = None
    try:
        sub_folder_id = client.create_folder(
            f"{config.preflight.test_filename}_folder", config.parent_folder_id
        )
        uploaded = client.upload_bytes(
            data=_make_test_jpeg(),
            name="01.jpg",
            parent_id=sub_folder_id,
            mime_type="image/jpeg",
        )
        report.add(Check("サブフォルダ + 画像アップロードテスト", True,
                         f"01.jpg を作成し {uploaded.size} バイトを確認"))
    except DriveQuotaError as exc:
        report.add(Check(
            "サブフォルダ + 画像アップロードテスト", False, str(exc),
            remedy="納品先を共有ドライブに変更してください（保存容量はドライブ側が持ちます）",
        ))
    except DriveError as exc:
        report.add(Check("サブフォルダ + 画像アップロードテスト", False, str(exc)))
    finally:
        if cleanup and sub_folder_id:
            _safe_delete(client, sub_folder_id, report)

    # 6. 既存 frmg_ig* フォルダの点検 -------------------------------------
    try:
        children = client.list_children(config.parent_folder_id)
        delivery_folders = [
            f for f in children
            if f.get("mimeType") == FOLDER_MIME
            and f.get("name", "").startswith(config.folder_prefix)
        ]
        empty: list[str] = []
        for folder_entry in delivery_folders:
            inner = client.list_children(folder_entry["id"], page_size=1)
            if not inner:
                empty.append(folder_entry["name"])
        if not delivery_folders:
            detail = f"{config.folder_prefix}* のフォルダはまだありません"
        elif empty:
            detail = (f"{len(delivery_folders)} 件中 {len(empty)} 件が空です: "
                      f"{', '.join(sorted(empty))}")
        else:
            detail = f"{len(delivery_folders)} 件すべてに中身があります"
        report.add(Check("既存納品フォルダの点検", not empty, detail,
                         remedy="空フォルダは納品失敗の痕跡です。原因解消後に再納品してください",
                         fatal=False))
    except DriveError as exc:
        report.add(Check("既存納品フォルダの点検", False, str(exc), fatal=False))

    return report


def _safe_delete(client: DriveClient, file_id: str, report: PreflightReport) -> None:
    """後片付け。失敗しても検査結果自体は落とさないが、必ずログに残す。"""
    try:
        client.delete(file_id)
    except DriveError as exc:
        log.warning("テスト用ファイル/フォルダの削除に失敗: id=%s (%s)", file_id, exc)
        report.add(Check("テスト用ファイルの後片付け", False,
                         f"id={file_id} の削除に失敗: {exc}",
                         remedy="Drive上に残ったテスト用ファイルを手動で削除してください",
                         fatal=False))


def format_report(report: PreflightReport) -> str:
    """人が読む用のサマリ。"""
    lines = ["", "=" * 68, " Drive 疎通確認レポート", "=" * 68]
    if report.account_email:
        lines.append(f" 認証アカウント: {report.account_email}")
        lines.append("-" * 68)
    for check in report.checks:
        mark = "OK  " if check.ok else ("NG  " if check.fatal else "WARN")
        lines.append(f" [{mark}] {check.name}")
        if check.detail:
            lines.append(f"        {check.detail}")
        if not check.ok and check.remedy:
            lines.append(f"        → 対処: {check.remedy}")
    lines.append("=" * 68)
    lines.append(" 結果: " + ("疎通OK。納品処理を実行できます。"
                              if report.ok else "疎通NG。上記の対処を行ってください。"))
    lines.append("=" * 68)
    return "\n".join(lines)
