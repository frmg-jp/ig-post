"""[3] 審査UI のテスト。

承認・非承認が DB に正しく反映され、非承認理由が feedback に残ることを
確かめる。理由の蓄積が [7] 学習ループの入力なので、ここが抜けると
学習が回らない。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from freming.collect.base import Candidate
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import insert_candidate
from freming.web.app import create_app


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "review.db"
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


@pytest.fixture()
def client(config):
    return TestClient(create_app(config))


def _add(conn, url="https://example.com/loft/", **overrides) -> int:
    data = {
        "source": "wowhaus",
        "source_rank": "A",
        "source_url": url,
        "title": "Former warehouse loft",
        "content_text": "A converted warehouse.",
        "for_sale_evidence": "for sale",
        "signal_score": 2,
        "is_for_sale": 1,
        "price": "$2,400,000",
        "location_city": "San Francisco",
        "location_country": "United States",
        "thumbnail_url": None,
    }
    data.update(overrides)
    property_id = insert_candidate(conn, Candidate(**data))
    conn.commit()
    return property_id


def _status(conn, property_id) -> str:
    return conn.execute(
        "SELECT status FROM properties WHERE id = ?", (property_id,)
    ).fetchone()["status"]


def test_list_shows_pending_candidates(client, conn) -> None:
    _add(conn)
    body = client.get("/").text
    assert "Former warehouse loft" in body
    assert "承認" in body


def test_approve_updates_status(client, conn) -> None:
    property_id = _add(conn)
    response = client.post(f"/p/{property_id}/approve", data={"status": "pending"})
    assert response.status_code == 200      # リダイレクト先まで辿った結果
    assert _status(conn, property_id) == "approved"


def test_reject_records_feedback(client, conn) -> None:
    """非承認理由が feedback に入ること（スコアリングの学習材料になる）。"""
    property_id = _add(conn)
    client.post(
        f"/p/{property_id}/reject",
        data={"reason": "前歴の痕跡が残っていない（内装だけのリノベ）", "status": "pending"},
    )
    assert _status(conn, property_id) == "rejected"

    rows = conn.execute("SELECT reason, property_id FROM feedback").fetchall()
    assert len(rows) == 1
    assert "痕跡が残っていない" in rows[0]["reason"]
    assert rows[0]["property_id"] == property_id


def test_reject_combines_preset_and_free_text(client, conn) -> None:
    property_id = _add(conn)
    client.post(
        f"/p/{property_id}/reject",
        data={"reason": "様式・築年が特定できない", "reason_free": "1990年代の建売に見える"},
    )
    reason = conn.execute("SELECT reason FROM feedback").fetchone()["reason"]
    assert "様式・築年が特定できない" in reason
    assert "1990年代の建売に見える" in reason


def test_reject_without_reason_is_refused(client, conn) -> None:
    """理由なしの非承認は受け付けない。学習の材料が失われるため。"""
    property_id = _add(conn)
    client.post(f"/p/{property_id}/reject", data={"reason": "", "reason_free": "  "})
    assert _status(conn, property_id) == "pending"
    assert conn.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()["n"] == 0


def test_delivered_candidates_cannot_be_changed(client, conn) -> None:
    """納品済みは審査し直せない（重複納品を防ぐ）。"""
    property_id = _add(conn)
    conn.execute("UPDATE properties SET status = 'delivered' WHERE id = ?", (property_id,))
    conn.commit()

    client.post(f"/p/{property_id}/approve")
    client.post(f"/p/{property_id}/reject", data={"reason": "やっぱり違う"})
    assert _status(conn, property_id) == "delivered"


def test_reset_returns_to_pending_but_keeps_feedback(client, conn) -> None:
    """誤操作の復旧。人が下した判断そのものは学習材料として残す。"""
    property_id = _add(conn)
    client.post(f"/p/{property_id}/reject", data={"reason": "画像が足りない、または品質が低い"})
    client.post(f"/p/{property_id}/reset")

    assert _status(conn, property_id) == "pending"
    assert conn.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()["n"] == 1


def test_detail_page_shows_collected_text(client, conn) -> None:
    property_id = _add(conn, content_text="Ghost signage remains on the brick facade.")
    body = client.get(f"/p/{property_id}").text
    assert "Ghost signage remains" in body


def test_unknown_property_returns_404(client) -> None:
    assert client.get("/p/9999").status_code == 404


def test_manual_entry_does_not_fetch_the_page(client, conn, monkeypatch) -> None:
    """手動投入では相手サイトに一切アクセスしないこと。"""
    from freming.collect import manual as manual_mod

    def _boom(*_args, **_kwargs):
        raise AssertionError("手動投入でHTTPクライアントを作ってはいけない")

    monkeypatch.setattr(manual_mod, "HttpClient", _boom)

    client.post(
        "/manual",
        data={
            "url": "https://www.zillow.com/homedetails/123/",
            "title": "Firehouse conversion",
            "price": "$1,250,000",
            "city": "Chicago",
        },
    )
    row = conn.execute(
        "SELECT * FROM properties WHERE source_url LIKE '%zillow%'"
    ).fetchone()
    assert row is not None
    assert row["is_for_sale"] == 1
    assert "手動入力" in row["for_sale_evidence"]


def test_score_breakdown_is_rendered(client, conn) -> None:
    """軸ごとの内訳が画面に出ること（なぜその点数かを人が確認できる）。"""
    property_id = _add(conn)
    conn.execute(
        "UPDATE properties SET score = 87.3, score_detail = ? WHERE id = ?",
        (
            '{"axes": [{"key": "story", "raw": 90, "weight": 0.25, "reason": ""},'
            ' {"key": "area", "raw": 100, "weight": 0.2, "reason": "San Francisco"}]}',
            property_id,
        ),
    )
    conn.commit()

    body = client.get("/").text
    assert "87.3" in body
    assert "story=90" in body
    assert "San Francisco" in body


def test_broken_score_detail_does_not_break_the_page(client, conn) -> None:
    """壊れた内訳データで一覧全体が落ちないこと。"""
    property_id = _add(conn)
    conn.execute(
        "UPDATE properties SET score = 50, score_detail = 'not json' WHERE id = ?",
        (property_id,),
    )
    conn.commit()
    assert client.get("/").status_code == 200


def test_rules_page_needs_explicit_approval(client, conn) -> None:
    """ルール候補は画面で承認するまで適用されないこと。"""
    from freming.db.repository import approved_rules

    conn.execute(
        "INSERT INTO rule_candidates (reason_tag, hit_count, proposal, state, created_at) "
        "VALUES ('no_visible_provenance', 4, '痕跡が残っていない物件は対象外とする', "
        "'proposed', datetime('now'))"
    )
    conn.commit()

    body = client.get("/rules").text
    assert "痕跡が残っていない物件は対象外とする" in body
    assert approved_rules(conn) == []

    client.post("/rules/no_visible_provenance/approve")
    assert approved_rules(conn) == ["痕跡が残っていない物件は対象外とする"]


def test_dismissing_a_rule_keeps_it_out_of_the_prompt(client, conn) -> None:
    from freming.db.repository import approved_rules

    conn.execute(
        "INSERT INTO rule_candidates (reason_tag, hit_count, proposal, state, created_at) "
        "VALUES ('area_mismatch', 5, 'エリア外は対象外とする', 'proposed', datetime('now'))"
    )
    conn.commit()

    client.post("/rules/area_mismatch/dismiss")
    assert approved_rules(conn) == []


def test_series_is_not_offered_any_more(client, conn) -> None:
    """連載企画は 2026-08-06 に畳んだ（config の series を空にした）。

    承認のたびに企画を選ぶ手間に見合う運用になっていなかったため。
    設定を戻せば復活するので、経路自体は残してある。
    """
    _add(conn)
    body = client.get("/").text
    assert "企画なし" not in body
    assert "FREMING Pick" not in body


def test_an_unknown_series_key_is_refused(client, conn) -> None:
    """企画を畳んだあとに古いリンクを叩かれても、未知の値を書き込まない。"""
    property_id = _add(conn)
    client.post(f"/p/{property_id}/series", data={"series": "freming_pick"})
    row = conn.execute(
        "SELECT series FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    assert row["series"] is None


def test_unknown_series_key_is_rejected(client, conn) -> None:
    """config.yaml に無い企画キーは保存しない。"""
    property_id = _add(conn)
    client.post(f"/p/{property_id}/series", data={"series": "made_up_series"})

    row = conn.execute(
        "SELECT series FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    assert row["series"] is None


def test_delivered_series_cannot_be_changed(client, conn) -> None:
    """納品済みのラベルは変えない（meta.txt と食い違うため）。"""
    property_id = _add(conn)
    conn.execute(
        "UPDATE properties SET status = 'delivered', series = 'hidden_gem' WHERE id = ?",
        (property_id,),
    )
    conn.commit()

    client.post(f"/p/{property_id}/series", data={"series": "freming_pick"})
    row = conn.execute(
        "SELECT series FROM properties WHERE id = ?", (property_id,)
    ).fetchone()
    assert row["series"] == "hidden_gem"


def test_empty_approved_tab_explains_what_to_do(client) -> None:
    """空の画面で、そのタブに出すための次の一手が分かること。"""
    body = client.get("/?status=approved").text
    assert "承認済みの物件はまだありません" in body
    assert "未審査" in body


def test_empty_pending_tab_suggests_collecting(client) -> None:
    body = client.get("/?status=pending").text
    assert "collect" in body and "score" in body


def test_delivered_card_links_to_drive(client, conn) -> None:
    """納品済みから Drive のフォルダを直接開けること。"""
    property_id = _add(conn)
    conn.execute("UPDATE properties SET status = 'delivered' WHERE id = ?", (property_id,))
    conn.execute(
        "INSERT INTO deliveries (property_id, folder_name, image_count, drive_folder_id, "
        "delivered_at) VALUES (?, 'frmg_ig001', 10, 'FOLDER123', datetime('now'))",
        (property_id,),
    )
    conn.commit()

    body = client.get("/?status=delivered").text
    assert "https://drive.google.com/drive/folders/FOLDER123" in body
    assert "frmg_ig001" in body


def test_area_is_shown_next_to_the_score(client, conn) -> None:
    """エリアは審査中に最も参照するので点数の隣に出す。"""
    property_id = _add(conn)
    conn.execute("UPDATE properties SET score = 88.4 WHERE id = ?", (property_id,))
    conn.commit()

    body = client.get("/").text
    assert 'class="area"' in body
    assert "San Francisco" in body


def test_breakdown_is_collapsed(client, conn) -> None:
    """内訳は畳んでおく（日々の審査で毎回読む情報ではない）。"""
    property_id = _add(conn)
    conn.execute(
        "UPDATE properties SET score = 50, score_detail = ? WHERE id = ?",
        ('{"axes": [{"key": "story", "raw": 90, "weight": 0.25, "reason": ""}]}', property_id),
    )
    conn.commit()

    body = client.get("/").text
    assert "<details class=\"axes\">" in body      # open 属性なし = 閉じている
    assert "判定の内訳" in body


def test_undelivered_card_has_no_drive_link(client, conn) -> None:
    _add(conn)
    assert "drive.google.com" not in client.get("/").text


def test_thumbnail_links_to_the_article(client, conn) -> None:
    """サムネイルから元記事を開けること。"""
    _add(conn, thumbnail_url="https://example.com/photos/hero.jpg")

    body = client.get("/").text
    # 画像が元記事へのリンクで包まれている
    assert '<a href="https://example.com/loft/' in body
    assert 'src="https://example.com/photos/hero.jpg"' in body
    assert body.index('href="https://example.com/loft/') < body.index(
        'src="https://example.com/photos/hero.jpg"'
    )


# ----------------------------------------------------------------------
# 承認から納品までの自動化
# ----------------------------------------------------------------------
def test_approve_wakes_the_delivery_worker(config, conn) -> None:
    """承認したら巡回間隔を待たずに納品が始まること。"""
    from freming.delivery.worker import DeliveryWorker

    worker = DeliveryWorker(config)
    client = TestClient(create_app(config, worker=worker))
    property_id = _add(conn)

    assert not worker._wakeup.is_set()
    client.post(f"/p/{property_id}/approve", data={"status": "pending"})
    assert worker._wakeup.is_set()


def test_approving_a_delivered_property_does_not_wake_the_worker(config, conn) -> None:
    """納品済みには何もしないので、ワーカーを起こす必要もない。"""
    from freming.delivery.worker import DeliveryWorker

    worker = DeliveryWorker(config)
    client = TestClient(create_app(config, worker=worker))
    property_id = _add(conn)
    conn.execute("UPDATE properties SET status = 'delivered' WHERE id = ?", (property_id,))
    conn.commit()

    client.post(f"/p/{property_id}/approve")
    assert not worker._wakeup.is_set()


def test_approved_card_shows_it_is_waiting_for_delivery(config, conn) -> None:
    from freming.delivery.worker import DeliveryWorker

    client = TestClient(create_app(config, worker=DeliveryWorker(config)))
    property_id = _add(conn)
    client.post(f"/p/{property_id}/approve")

    body = client.get("/?status=approved").text
    assert "納品待ち" in body
    assert "自動納品 ON" in body


def test_failed_delivery_is_shown_with_a_retry_button(config, conn) -> None:
    """失敗しても承認一覧に残り、そこから再開できること。"""
    from freming.db.repository import record_delivery_failure
    from freming.delivery.worker import DeliveryWorker

    client = TestClient(create_app(config, worker=DeliveryWorker(config)))
    property_id = _add(conn)
    client.post(f"/p/{property_id}/approve")
    for _ in range(config.delivery.max_attempts):
        record_delivery_failure(conn, property_id, "NoImagesFound: 加工できた画像がありません")

    body = client.get("/?status=approved").text
    assert "納品に失敗" in body
    assert "加工できた画像がありません" in body
    assert "納品を再試行" in body

    client.post(f"/p/{property_id}/retry-delivery", data={"status": "approved"})
    row = conn.execute(
        "SELECT delivery_attempts, delivery_error FROM properties WHERE id = ?",
        (property_id,),
    ).fetchone()
    assert row["delivery_attempts"] == 0
    assert row["delivery_error"] is None


def test_pending_tab_does_not_auto_refresh(config, conn) -> None:
    """審査中に勝手にページが変わると、選択位置が飛んで操作の邪魔になる。"""
    from freming.delivery.worker import DeliveryWorker

    client = TestClient(create_app(config, worker=DeliveryWorker(config)))
    property_id = _add(conn)
    client.post(f"/p/{property_id}/approve")

    assert "http-equiv=\"refresh\"" not in client.get("/?status=pending").text
    assert "http-equiv=\"refresh\"" in client.get("/?status=approved").text


def test_auto_delivery_off_falls_back_to_the_cli_guidance(config, conn) -> None:
    config.delivery.auto = False
    client = TestClient(create_app(config))
    _add(conn)

    body = client.get("/?status=delivered").text
    assert "自動納品 ON" not in body
    assert "freming.cli deliver" in body


def test_area_shows_a_country_flag(client, conn) -> None:
    """審査中は文字を読む前に「どこの国か」が分かるようにする。"""
    _add(conn, location_city="Lisbon", location_country="Portugal")
    body = client.get("/").text
    assert "🇵🇹" in body
    assert 'class="flag"' in body


def test_unknown_country_shows_no_flag(client, conn) -> None:
    """当てずっぽうの旗を出さない。所在地は審査の判断材料なので誤りは害になる。"""
    _add(conn, location_city=None, location_country=None)
    body = client.get("/").text
    assert 'class="flag"' not in body
    assert "所在地不明" in body


def test_thumbnail_size_comes_from_the_config(config, conn) -> None:
    """review_ui.thumbnail_px が実際に効くこと。

    以前は thumbnail_max_px という名前でテンプレートに渡していたが、
    どこでも使われておらず、値を変えても何も起きなかった。
    """
    config.review_ui.thumbnail_px = 240
    client = TestClient(create_app(config))
    _add(conn, thumbnail_url="https://example.com/photos/hero.jpg")

    assert "grid-template-columns: 240px 1fr" in client.get("/").text


def test_thumbnail_is_square(client, conn) -> None:
    """縦横比を保つと段の高さが揃わず、一覧が読みにくくなる。"""
    _add(conn, thumbnail_url="https://example.com/photos/hero.jpg")
    body = client.get("/").text
    assert "aspect-ratio: 1" in body
    assert "object-fit: cover" in body


def test_price_is_shown_next_to_the_score(client, conn) -> None:
    """価格は審査の判断に直接効くので、点数の隣（国旗の左）に出す。"""
    property_id = _add(conn, price="£1,500,000")
    conn.execute("UPDATE properties SET score = 66.8 WHERE id = ?", (property_id,))
    conn.commit()

    body = client.get("/").text
    assert '<span class="price">£1,500,000</span>' in body
    # 見出しに出したので、タグ列には重ねて出さない
    assert '<span class="tag">£1,500,000</span>' not in body
    # 並びは 点数 → 価格 → 国旗
    assert body.index('class="price"') < body.index('class="flag"')


def test_no_price_leaves_the_head_clean(client, conn) -> None:
    _add(conn, price=None)
    assert 'class="price"' not in client.get("/").text


def test_reason_dropdown_has_a_fixed_width(client, conn) -> None:
    """選択肢の文が長いと、放っておくと行の大半を占める。"""
    _add(conn)
    assert ".actions select { width: 170px" in client.get("/").text


def test_the_favicon_is_served(client) -> None:
    """ファビコンは frmg.jp の実物を同梱している。

    パッケージのデータとして配る（pyproject の package-data）ので、
    配布物から抜けると 404 になる。タブの見分けが付かなくなるだけだが、
    気づきにくいのでここで止める。
    """
    response = client.get("/static/favicon.ico")
    assert response.status_code == 200
    assert response.content[:4] == b"\x00\x00\x01\x00"   # ICO のシグネチャ

    apple = client.get("/static/apple-touch-icon.png")
    assert apple.status_code == 200
    assert apple.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_pages_point_at_the_favicon(client) -> None:
    assert '<link rel="icon" type="image/x-icon" href="/static/favicon.ico">' in client.get("/").text


# ----------------------------------------------------------------------
# 並べ替えバーと、連載企画を畳んだこと。


def test_sort_bar_replaces_the_series_bar(client) -> None:
    body = client.get("/?status=pending").text
    assert "並べ替え:" in body
    # 企画の絞り込みバーは消えている
    assert "企画:" not in body


def test_all_sort_options_are_offered(client) -> None:
    body = client.get("/?status=pending").text
    for label in ("スコア順", "新着順", "古い順", "価格が高い順", "価格が安い順", "築年数が古い順"):
        assert label in body, label


def test_series_dropdown_is_gone_from_the_cards(client, conn) -> None:
    """config の series を空にしたので、承認時に企画を選ぶ必要がない。"""
    _add(conn)
    body = client.get("/?status=pending").text
    assert "企画なし" not in body


def test_the_new_reject_reason_is_offered(client, conn) -> None:
    """理由のプルダウンはカードの中にあるので、候補が1件要る。"""
    _add(conn)
    assert "なんとなくダサい" in client.get("/?status=pending").text


def test_an_unknown_sort_does_not_break_the_page(client) -> None:
    """SORTS の値は SQL に埋まる。未知の値で 500 にならないこと。"""
    assert client.get("/?sort=' OR 1=1--").status_code == 200
    assert client.get("/?sort=nonsense").status_code == 200
