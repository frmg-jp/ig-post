"""[3] 審査UI。

    一覧（スコア順） → 承認 / 非承認（理由必須） → feedback に蓄積

ローカル（127.0.0.1）で人が使う前提で、認証は持たない。外部に公開する
用途は想定していないため、config の host も既定で 127.0.0.1 のままにする。

単体実行:
    python -m freming.web.app
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from freming.config import Config, load_config
from freming.db.connection import connect
from freming.db.repository import (
    approve_property,
    count_by_status,
    decide_rule_candidate,
    get_property,
    list_properties,
    list_rule_candidates,
    reject_property,
    reset_review,
)
from freming.logging_setup import get_logger, setup_logging

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# 非承認の定型理由。毎回自由入力させると表記がばらつき、[7] の
# タグ分類が効かなくなる。よく使うものを固定文にしておく。
REJECT_PRESETS = [
    "前歴の痕跡が残っていない（内装だけのリノベ）",
    "様式・築年が特定できない",
    "一点物ではない（分譲・同一仕様が複数）",
    "売出中ではない（記事の価格は建設費/落札額）",
    "画像が足りない、または品質が低い",
    "既出・類似の物件を納品済み",
]


def _axes(row: sqlite3.Row) -> list[dict]:
    """score_detail から軸ごとの内訳を取り出す。未採点なら空。"""
    raw = row["score_detail"]
    if not raw:
        return []
    try:
        return json.loads(raw).get("axes", [])
    except (ValueError, TypeError):
        log.warning("score_detail を解釈できません: property_id=%s", row["id"])
        return []


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    app = FastAPI(title="FREMING CURATED 審査")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["axes"] = _axes

    def _conn() -> sqlite3.Connection:
        return connect(config.app.db_path)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, status: str | None = None, page: int = 1):
        status = status or config.review_ui.default_filter
        page = max(page, 1)
        size = config.review_ui.page_size
        conn = _conn()
        try:
            rows = list_properties(
                conn, status=status, limit=size, offset=(page - 1) * size
            )
            counts = count_by_status(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(
            request,
            "list.html",
            {
                "rows": rows,
                "counts": counts,
                "status": status,
                "page": page,
                "has_next": len(rows) == size,
                "presets": REJECT_PRESETS,
                "highlight": config.scoring.thresholds.highlight_above,
                "thumb_px": config.review_ui.thumbnail_max_px,
            },
        )

    @app.get("/p/{property_id}", response_class=HTMLResponse)
    def detail(request: Request, property_id: int):
        conn = _conn()
        try:
            row = get_property(conn, property_id)
        finally:
            conn.close()
        if row is None:
            return HTMLResponse("見つかりません", status_code=404)
        return templates.TemplateResponse(
            request,
            "detail.html",
            {"row": row, "presets": REJECT_PRESETS},
        )

    @app.post("/p/{property_id}/approve")
    def approve(property_id: int, status: str = Form("pending")):
        conn = _conn()
        try:
            if not approve_property(conn, property_id):
                log.warning("承認できませんでした（納品済みの可能性）: id=%s", property_id)
        finally:
            conn.close()
        return RedirectResponse(f"/?status={status}", status_code=303)

    @app.post("/p/{property_id}/reject")
    def reject(
        property_id: int,
        reason: str = Form(""),
        reason_free: str = Form(""),
        status: str = Form("pending"),
    ):
        # 定型理由と自由入力の両方が来たら、自由入力を優先して両方残す。
        # 具体的な文言の方が [7] の学習材料として価値がある。
        text = " / ".join(p for p in (reason.strip(), reason_free.strip()) if p)
        if not text:
            return RedirectResponse(
                f"/p/{property_id}?error=reason_required", status_code=303
            )
        conn = _conn()
        try:
            reject_property(conn, property_id, text)
        finally:
            conn.close()
        return RedirectResponse(f"/?status={status}", status_code=303)

    @app.post("/p/{property_id}/reset")
    def reset(property_id: int, status: str = Form("pending")):
        conn = _conn()
        try:
            reset_review(conn, property_id)
        finally:
            conn.close()
        return RedirectResponse(f"/?status={status}", status_code=303)

    @app.get("/rules", response_class=HTMLResponse)
    def rules(request: Request):
        """[7] のルール候補。自動適用しないので、ここが唯一の適用経路。"""
        conn = _conn()
        try:
            rows = list_rule_candidates(conn)
            counts = count_by_status(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(
            request,
            "rules.html",
            {"rows": rows, "counts": counts, "status": "rules"},
        )

    @app.post("/rules/{tag}/{decision}")
    def decide_rule(tag: str, decision: str):
        if decision not in ("approve", "dismiss"):
            return HTMLResponse("不正な操作です", status_code=400)
        conn = _conn()
        try:
            decide_rule_candidate(
                conn, tag, "approved" if decision == "approve" else "dismissed"
            )
        finally:
            conn.close()
        return RedirectResponse("/rules", status_code=303)

    @app.post("/manual")
    def manual(
        url: str = Form(...),
        title: str = Form(...),
        price: str = Form(""),
        city: str = Form(""),
        country: str = Form(""),
        note: str = Form(""),
    ):
        """手動URL投入。Zillow / Redfin / Compass はこの経路だけで扱う。

        ページの自動取得は行わない（規約で自動収集が禁止されているため）。
        人が内容を確認した上で登録する前提の入口。
        """
        from freming.collect.manual import AlreadyCollected, add_manual_entry

        try:
            add_manual_entry(
                config,
                source_url=url.strip(),
                title=title.strip(),
                price=price.strip() or None,
                city=city.strip() or None,
                country=country.strip() or None,
                note=note.strip() or None,
            )
        except AlreadyCollected:
            log.info("既に登録済みのURLです: %s", url)
        return RedirectResponse("/?status=pending", status_code=303)

    return app


def main() -> int:
    import uvicorn

    config = load_config()
    setup_logging(config.app.log_dir, config.app.log_level)
    log.info(
        "審査UIを起動します: http://%s:%d", config.review_ui.host, config.review_ui.port
    )
    uvicorn.run(
        create_app(config),
        host=config.review_ui.host,
        port=config.review_ui.port,
        log_level=config.app.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
