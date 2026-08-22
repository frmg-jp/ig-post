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
from freming.db.migrate import is_missing_table
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
from freming.fx import effective_rates
from freming.instagram.worker import PostingWorker
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
    # [9] 投稿ワーカー。**画像を配るのが審査UI自身**なので、投稿する側も
    # ここに置く。auto_post を立てるのは1箇所だけにすること。
    poster = PostingWorker(config) if config.instagram.auto_post else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if worker is not None:
            worker.start()
        if poster is not None:
            poster.start()
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()
            if poster is not None:
                poster.stop()

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
            # レートはDB（定期実行が週1で更新）→ config の順に見る。
            fx_rates, fx_as_of = effective_rates(conn, config)
            rows = list_properties(
                conn, status=status, limit=size, offset=(page - 1) * size,
                series=series, sort=sort, fx_rates=fx_rates,
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
                "fx_as_of": fx_as_of,
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

    def _needs_migration(request: Request, name: str, status_key: str) -> HTMLResponse:
        """新しい画面に必要な表がまだ無いときの案内。

        マイグレーションを流すのは定期実行（毎朝 09:00 JST）だが、コードは
        push のたびに配られる。**新しいコードが先に動き出す時間帯がある。**
        そこで 500 を返すと原因が分からないので、やることを出す。
        """
        log.warning("%s: マイグレーションが未適用のため表示できません", name)
        return templates.TemplateResponse(
            request,
            "needs_migration.html",
            {"name": name, "status": status_key, "counts": {}},
        )

    # ------------------------------------------------------------------
    # [9] Instagram への投稿
    # ------------------------------------------------------------------
    @app.get("/m/{token}")
    def post_media(token: str):
        """投稿する画像を配る。**Meta がここへ取りに来る。**

        認証は通さない（web/auth.py の EXEMPT_PREFIXES）。相手に資格情報を
        渡す方法がないため。代わりに token を推測できない文字列にしてある。
        投稿が済んだ行は消すので、URLはすぐ死ぬ。
        """
        from fastapi.responses import Response

        from freming.instagram.media import load_media

        conn = _conn()
        try:
            found = load_media(conn, token)
        finally:
            conn.close()
        if found is None:
            return Response("見つかりません", status_code=404)
        content, mime = found
        # Meta は取得のたびに取りに来る。キャッシュは短くてよい。
        return Response(content, media_type=mime, headers={"Cache-Control": "public, max-age=600"})

    @app.get("/schedule", response_class=HTMLResponse)
    def schedule(request: Request, view: str = "todo"):
        """投稿予定。承認がそのまま公開にならないよう、人が見て止める場所。

        view=todo は予定（これから出るもの・見送り・失敗）、
        view=done は投稿済み。出たものが予定の列に混ざると読みにくい。
        """
        from datetime import UTC, datetime, timedelta
        from urllib.parse import quote
        from zoneinfo import ZoneInfo

        from freming.db.repository import scheduled_posts

        zone = ZoneInfo(config.instagram.timezone)
        now = datetime.now(UTC)
        until = now + timedelta(days=config.instagram.plan_days)
        conn = _conn()
        try:
            rows = list(scheduled_posts(conn, until.isoformat()))
            counts = count_by_status(conn)
        except Exception as exc:
            if not is_missing_table(exc):
                raise
            return _needs_migration(request, "投稿予定", "schedule")
        finally:
            conn.close()

        # 投稿に使う写真の一覧。並び替えのUIに出す。image_order があれば
        # その順（投稿ワーカーと同じ解釈）。サムネイルは取得元のURLを
        # そのまま使う（審査画面の thumbnail_url と同じ扱い）。
        from freming.instagram.worker import _parse_image_order

        def _col(row, key):
            """マイグレーション前の行にも耐える読み方。無い列は None。"""
            try:
                return row[key]
            except (KeyError, IndexError):
                return None

        def _images_for(row) -> list[dict]:
            if row["kind"] != "feed" or not row["property_id"]:
                return []
            if row["state"] not in ("planned", "failed", "skipped"):
                return []
            conn2 = _conn()
            try:
                found = conn2.execute(
                    "SELECT position, source_url FROM images "
                    "WHERE property_id = ? AND position IS NOT NULL ORDER BY position",
                    (row["property_id"],),
                ).fetchall()
            finally:
                conn2.close()
            by_position = {int(r["position"]): r["source_url"] for r in found}
            order = [p for p in _parse_image_order(_col(row, "image_order"))
                     if p in by_position]
            order += [p for p in sorted(by_position) if p not in order]
            limit = config.instagram.carousel_max
            return [
                {"position": p, "url": by_position[p], "used": index < limit}
                for index, p in enumerate(order)
            ]

        def _address(row) -> str:
            """検索に使う住所。記事から抽出した番地＋市＋州＋国。

            番地が取れていない行（記事に住所を書いていない編集メディアや、
            抽出前の古い行）は、タイトルが住所そのもののリスティングなら
            それを使う。どちらも無ければ空。
            """
            street = _col(row, "street_address") or ""
            if not street:
                title = (row["title"] or "").split(" - MLS")[0].strip()
                head = title.split()[0].rstrip("-") if title.split() else ""
                # 「209 Java Drive #K, Briny Breezes, FL 33435」の形かどうか
                return title if ("," in title and head.isdigit()) else ""
            parts = [
                street,
                row["location_city"] or "",
                _col(row, "location_region") or "",
                row["location_country"] or "",
            ]
            return ", ".join(p for p in parts if p)

        def _search_links(row) -> dict:
            """住所からの検索リンク。**開くのは人のブラウザ**で、
            こちらから Zillow を叩くことはしない。"""
            address = _address(row)
            if not address:
                return {}
            return {
                "address": address,
                # Zillow 内の検索。住所そのものを渡すのでほぼ直接着く
                "zillow": f"https://www.zillow.com/homes/{quote(address)}_rb/",
                # 検索エンジン経由。Zillow に無い物件でも仲介ページに当たる
                "web": f"https://duckduckgo.com/?q={quote(address + ' zillow')}",
            }

        def _display_title(row) -> str:
            """一覧の見出し。短い物件名が無い行は記事タイトルを整形する。

            住所タイトルのリスティングは「〜 - MLS# ... - 仲介名」まで
            付いてくる。見出しに要るのは住所までなので、そこで切る。
            """
            name = _col(row, "display_name")
            if name:
                return name
            title = row["title"] or ""
            for marker in (" - MLS", " – MLS", " | MLS"):
                if marker in title:
                    title = title.split(marker)[0]
                    break
            return title

        view = "done" if view == "done" else "todo"
        rows = [r for r in rows if r["state"] != "deleted"]
        done_count = sum(1 for r in rows if r["state"] == "published")
        todo_count = len(rows) - done_count
        rows = [r for r in rows if (r["state"] == "published") == (view == "done")]
        if view == "done":
            # 投稿済みは新しい順。予定は時系列のまま。
            rows = sorted(rows, key=lambda r: r["scheduled_at"], reverse=True)

        days: dict[str, list] = {}
        for row in rows:
            moment = datetime.fromisoformat(row["scheduled_at"]).astimezone(zone)
            days.setdefault(moment.strftime("%m/%d (%a)"), []).append(
                {
                    "row": row,
                    "at": moment.strftime("%H:%M"),
                    "day": moment.strftime("%m/%d (%a)"),
                    "at_input": moment.strftime("%Y-%m-%dT%H:%M"),
                    "images": _images_for(row),
                    "caption_edited": bool(_col(row, "caption_edited_at")),
                    "name": _display_title(row),
                    "listing_url": _col(row, "listing_url") or "",
                    "search": _search_links(row) if row["property_id"] else {},
                }
            )
        return templates.TemplateResponse(
            request,
            "schedule.html",
            {
                "days": days,
                "days_ahead": config.instagram.plan_days,
                "carousel_max": config.instagram.carousel_max,
                "view": view,
                "todo_count": todo_count,
                "done_count": done_count,
                "auto_post": config.instagram.auto_post,
                "ready": bool(config.instagram.public_base_url),
                "counts": counts,
                "status": "schedule",
            },
        )

    @app.get("/stories", response_class=HTMLResponse)
    def stories(request: Request, day: int = 0):
        """ストーリーズに手で追加するための一覧。

        **API は「投稿をストーリーズに追加」を開けていない。** リンク・
        メンション・アンケートといったスタンプは一切投稿できず、
        ストーリーズはキャプションすら受け付けない。タップで投稿へ飛ぶ
        カードにするには、アプリで人がやるしかない。

        この画面は、その手作業から考える要素を無くすためのもの。
        スマホで開いて「投稿を開く → 紙飛行機 → ストーリーズに追加」。
        """
        from datetime import UTC, datetime, time, timedelta
        from zoneinfo import ZoneInfo

        from freming.db.repository import posts_awaiting_story

        zone = ZoneInfo(config.instagram.timezone)
        target = (datetime.now(UTC).astimezone(zone) + timedelta(days=day)).date()
        start = datetime.combine(target, time.min, tzinfo=zone).astimezone(UTC)
        conn = _conn()
        try:
            rows = list(
                posts_awaiting_story(
                    conn, start.isoformat(), (start + timedelta(days=1)).isoformat()
                )
            )
            counts = count_by_status(conn)
        except Exception as exc:
            if not is_missing_table(exc):
                raise
            return _needs_migration(request, "ストーリーズ", "stories")
        finally:
            conn.close()

        def _maybe(row, key):
            try:
                return row[key]
            except (KeyError, IndexError):
                return None

        items = [
            {
                "row": row,
                "at": datetime.fromisoformat(row["published_at"]).astimezone(zone).strftime("%H:%M"),
                "done": bool(row["story_shared_at"]),
                "listing_url": _maybe(row, "listing_url") or "",
            }
            for row in rows
        ]
        return templates.TemplateResponse(
            request,
            "stories.html",
            {
                "items": items,
                "date_label": target.strftime("%m/%d (%a)"),
                "day": day,
                "done": sum(1 for i in items if i["done"]),
                "counts": counts,
                "status": "stories",
            },
        )

    @app.post("/posts/{post_id}/story-shared")
    def story_shared(post_id: int, shared: str = Form("1"), day: int = Form(0)):
        from freming.db.repository import mark_story_shared

        conn = _conn()
        try:
            mark_story_shared(conn, post_id, shared == "1")
        finally:
            conn.close()
        return RedirectResponse(f"/stories?day={day}", status_code=303)

    @app.post("/posts/{post_id}/skip")
    def skip_post_route(post_id: int):
        from freming.db.repository import skip_post

        conn = _conn()
        try:
            skip_post(conn, post_id)
        finally:
            conn.close()
        return RedirectResponse("/schedule", status_code=303)

    @app.post("/posts/{post_id}/retry")
    def retry_post_route(post_id: int):
        from freming.db.repository import retry_post

        conn = _conn()
        try:
            retry_post(conn, post_id)
        finally:
            conn.close()
        return RedirectResponse("/schedule", status_code=303)

    # ------------------------------------------------------------------
    # 投稿予定の編集（写真の並び・時刻・本文）。出す前に人が直す場所。
    # どれも planned / failed のときだけ効く（repository 側で守っている）。
    # ------------------------------------------------------------------
    @app.post("/posts/{post_id}/delete")
    def delete_post_route(post_id: int):
        """予定から消す。**候補には戻さない**（行は deleted として残し、
        この物件が再び予定に載るのを防ぐ）。重複や誤登録を外すためのもの。
        投稿中の行だけは触らない。"""
        conn = _conn()
        try:
            conn.execute(
                "UPDATE posts SET state = 'deleted' "
                "WHERE id = ? AND state != 'publishing'",
                (post_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse("/schedule", status_code=303)

    @app.post("/p/{property_id}/display-name")
    def display_name_route(property_id: int, name: str = Form("")):
        """表示名（投稿の見出し）を人が付ける。

        記事が薄くて抽出できなかった物件（住所タイトルのリスティング）用。
        空で保存すると消え、記事タイトルの整形表示に戻る。
        """
        text = name.strip()
        conn = _conn()
        try:
            conn.execute(
                "UPDATE properties SET display_name = ? WHERE id = ?",
                (text or None, property_id),
            )
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse("/schedule", status_code=303)

    @app.post("/p/{property_id}/listing-url")
    def listing_url_route(property_id: int, url: str = Form("")):
        """販売ページのURLを物件に持たせる。人が探して貼る（自動では探さない）。

        空で保存すると消える。http(s) 以外は受け付けない。
        """
        text = url.strip()
        if text and not text.startswith(("http://", "https://")):
            return RedirectResponse("/schedule", status_code=303)
        conn = _conn()
        try:
            conn.execute(
                "UPDATE properties SET listing_url = ? WHERE id = ?",
                (text or None, property_id),
            )
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse("/schedule", status_code=303)

    @app.post("/posts/{post_id}/order")
    def order_post_route(request: Request, post_id: int, order: str = Form("")):
        from fastapi.responses import Response

        from freming.db.repository import set_image_order
        from freming.instagram.worker import _parse_image_order

        # 受け取った並びを一度解釈し直す。壊れた値は既定の並びに戻す。
        parsed = _parse_image_order(order)
        conn = _conn()
        try:
            set_image_order(conn, post_id, ",".join(map(str, parsed)) or None)
        finally:
            conn.close()
        # 画面のドラッグからは裏で保存する（fetch）。ページを動かさない。
        if request.headers.get("x-requested-with") == "fetch":
            return Response(status_code=204)
        return RedirectResponse("/schedule", status_code=303)

    @app.post("/posts/{post_id}/caption")
    def caption_post_route(post_id: int, caption: str = Form("")):
        from freming.db.repository import set_caption

        text = caption.replace("\r\n", "\n").strip()
        if text:
            conn = _conn()
            try:
                # 人が直した印が付き、以後の replan は上書きしない。
                set_caption(conn, post_id, text, edited=True)
            finally:
                conn.close()
        return RedirectResponse("/schedule", status_code=303)

    @app.post("/posts/{post_id}/time")
    def time_post_route(post_id: int, at: str = Form("")):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from freming.db.repository import set_scheduled_at

        try:
            local = datetime.fromisoformat(at)
        except ValueError:
            return RedirectResponse("/schedule", status_code=303)
        if local.tzinfo is None:
            # datetime-local の値は無タイムゾーン。審査UIの時刻＝JSTで解釈する。
            local = local.replace(tzinfo=ZoneInfo(config.instagram.timezone))
        from datetime import UTC

        conn = _conn()
        try:
            set_scheduled_at(conn, post_id, local.astimezone(UTC).isoformat())
        finally:
            conn.close()
        return RedirectResponse("/schedule", status_code=303)

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
