"""[3] 審査UI。

    一覧（スコア順） → 承認 / 非承認（理由必須） → feedback に蓄積

既定はローカル（127.0.0.1）で人が使う前提で、認証を持たない。config の
host も 127.0.0.1 のままにしてある。担当者と共有するために外へ出すときは
Basic 認証をかける（web/auth.py）。認証なしで外向けに待ち受ける経路は
作らない。

単体実行:
    python -m freming.web.app
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from freming.config import Config, load_config
from freming.db.connection import DbConnection, Row, connect
from freming.db.repository import (
    DEFAULT_SORT,
    SORTS,
    approve_property,
    count_by_status,
    decide_rule_candidate,
    delivery_queue_size,
    get_property,
    list_properties,
    list_rule_candidates,
    reject_property,
    reset_review,
    retry_delivery,
    set_series,
)
from freming.delivery.worker import DeliveryWorker
from freming.logging_setup import get_logger, setup_logging
from freming.web.auth import BasicAuth, BasicAuthMiddleware, credentials_from_env
from freming.web.flags import flag

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
# ファビコンなどの静的ファイル。frmg.jp の実物を置いてある。
# pyproject の package-data に web/static/* を入れてあるので配布物にも入る。
STATIC_DIR = Path(__file__).parent / "static"

# 並べ替えの選択肢。キーは repository.SORTS と対応する。
SORT_LABELS: list[tuple[str, str]] = [
    ("score", "スコア順"),
    ("newest", "新着順"),
    ("oldest", "古い順"),
    ("price_desc", "価格が高い順"),
    ("price_asc", "価格が安い順"),
    ("built_oldest", "築年数が古い順"),
]

# 非承認の定型理由。毎回自由入力させると表記がばらつき、[7] の
# タグ分類が効かなくなる。よく使うものを固定文にしておく。
REJECT_PRESETS = [
    "前歴の痕跡が残っていない（内装だけのリノベ）",
    "様式・築年が特定できない",
    "一点物ではない（分譲・同一仕様が複数）",
    "売出中ではない（記事の価格は建設費/落札額）",
    "画像が足りない、または品質が低い",
    "既出・類似の物件を納品済み",
    # 言語化しにくい違和感も理由として残す。学習ループは理由をタグに
    # 分類するので、繰り返されればここから傾向が見えてくる。
    "なんとなくダサい",
]


def _axes(row: Row) -> list[dict]:
    """score_detail から軸ごとの内訳を取り出す。未採点なら空。"""
    raw = row["score_detail"]
    if not raw:
        return []
    try:
        return json.loads(raw).get("axes", [])
    except (ValueError, TypeError):
        log.warning("score_detail を解釈できません: property_id=%s", row["id"])
        return []


def create_app(
    config: Config | None = None,
    worker: DeliveryWorker | None = None,
    auth: BasicAuth | None = None,
) -> FastAPI:
    config = config or load_config()
    # 資格情報があれば全経路に認証をかける。ローカル（127.0.0.1）では
    # 未設定のままでよい。外向けに待ち受けるときの必須化は呼び出し側
    # （cli の serve と web/asgi.py）が web/auth.py の require_credentials で行う。
    auth = auth or credentials_from_env()
    # 承認したものを自動で納品するワーカー。審査UIと同じプロセスで動かすので、
    # 別ターミナルで deliver を叩く必要がない。
    auto = config.delivery.auto and config.drive.enabled
    worker = worker or (DeliveryWorker(config) if auto else None)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if worker is not None:
            worker.start()
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()

    app = FastAPI(title="FREMING CURATED 審査", lifespan=lifespan)
    if auth is not None:
        app.add_middleware(BasicAuthMiddleware, auth=auth)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["axes"] = _axes
    templates.env.filters["flag"] = flag

    def _conn() -> DbConnection:
        return connect(config.app.target())

    @app.get("/healthz")
    def healthz():
        """ホスティング側の死活監視用。認証を通さないので中身は返さない。"""
        return {"status": "ok"}

    @app.get("/ig/callback", response_class=HTMLResponse)
    def instagram_callback(
        request: Request, code: str | None = None, error_description: str | None = None
    ):
        """Instagram の認可後の着地先。

        @frmg.jpn の管理者に自分の端末で認可してもらうための受け皿。
        認証を通さない（web/auth.py の EXEMPT_PATHS）。理由は、その人が
        審査UIの資格情報を持っていないため。ここで Basic 認証を出すと
        パスワードを聞かれて詰む。

        **受け取った code はサーバー側で使わない。** code をトークンに
        交換するには app secret が要り、それは公開ホストに置かない方針
        （web/asgi.py と同じ理由）。画面に出して、手元の Mac で
        `instagram exchange-code` に渡してもらう。

        code は一度きり・短時間で失効するので、画面に出しても後から
        使い回せない。
        """
        return templates.TemplateResponse(
            request,
            "ig_callback.html",
            {"code": code, "error": error_description},
        )

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        status: str | None = None,
        page: int = 1,
        series: str | None = None,
        sort: str | None = None,
    ):
        status = status or config.review_ui.default_filter
        # 未知の値は既定に落とす。SORTS のキーはSQLに直接埋まるため。
        sort = sort if sort in SORTS else DEFAULT_SORT
        page = max(page, 1)
        size = config.review_ui.page_size
        conn = _conn()
        try:
            rows = list_properties(
                conn, status=status, limit=size, offset=(page - 1) * size,
                series=series, sort=sort,
            )
            counts = count_by_status(conn)
            queued = (
                delivery_queue_size(conn, config.delivery.max_attempts)
                if worker is not None
                else 0
            )
        finally:
            conn.close()
        return templates.TemplateResponse(
            request,
            "list.html",
            {
                "rows": rows,
                "counts": counts,
                "status": status,
                "auto_deliver": worker is not None,
                "delivering_id": worker.current_property_id if worker else None,
                # 納品の進み具合を見るタブだけ自動更新する。未審査タブで
                # 勝手にページが変わると、選択位置が飛んで審査の邪魔になる。
                "refresh_sec": (
                    15
                    if (worker is not None and queued and status in ("approved", "delivered"))
                    else 0
                ),
                "queued": queued,
                "max_attempts": config.delivery.max_attempts,
                "page": page,
                "has_next": len(rows) == size,
                "presets": REJECT_PRESETS,
                "series_options": config.series,
                "series": series,
                "sort": sort,
                "sort_options": SORT_LABELS,
                "highlight": config.scoring.thresholds.highlight_above,
                "thumb_px": config.review_ui.thumbnail_px,
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
            {
                "row": row,
                "presets": REJECT_PRESETS,
                "series_options": config.series,
                "auto_deliver": worker is not None,
                "delivering_id": worker.current_property_id if worker else None,
                "max_attempts": config.delivery.max_attempts,
                "thumb_px": config.review_ui.thumbnail_px,
            },
        )

    @app.post("/p/{property_id}/approve")
    def approve(property_id: int, status: str = Form("pending")):
        conn = _conn()
        try:
            approved = approve_property(conn, property_id)
            if not approved:
                log.warning("承認できませんでした（納品済みの可能性）: id=%s", property_id)
        finally:
            conn.close()
        # 巡回間隔を待たずに納品を始める
        if approved and worker is not None:
            worker.wake()
        return RedirectResponse(f"/?status={status}", status_code=303)

    @app.post("/p/{property_id}/retry-delivery")
    def retry(property_id: int, status: str = Form("approved")):
        """納品の試行回数を戻して自動納品に復帰させる。

        画像が消えていた・Driveが落ちていた等で上限まで失敗した候補は、
        原因を直したあとここから再開する。
        """
        conn = _conn()
        try:
            ok = retry_delivery(conn, property_id)
        finally:
            conn.close()
        if ok and worker is not None:
            worker.wake()
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

    @app.post("/p/{property_id}/series")
    def assign_series(
        property_id: int, series: str = Form(""), status: str = Form("pending")
    ):
        """連載企画のラベルを付ける。判定はせず、人が選んだものをそのまま保存する。"""
        key = series.strip()
        if key and not config.is_known_series(key):
            log.warning("config.yaml に無い企画キーです: %s", key)
            return RedirectResponse(f"/?status={status}", status_code=303)
        conn = _conn()
        try:
            set_series(conn, property_id, key or None)
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
