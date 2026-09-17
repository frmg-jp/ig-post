"""[10] 週次レポートのテスト。

**出したものがどうだったか**を読む画面。最初は未審査の候補を並べて
いたが、それは未審査タブの仕事だった（2026-09-16 の指摘）。

確かめるのは、数字の作り方（未取得を 0 に混ぜない）と、振り返りの
チェックが「出したもの」を見ていること。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from freming.collect.base import Candidate
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import insert_candidate
from freming.report.weekly import build, render, week_bounds
from freming.web.app import create_app

# 2026-09-16（水）。この週は 09/14（月）〜 09/20（日）。
NOW = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "report.db"
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


def _add(conn, url, *, score=None, genre=None, country="United States", **extra) -> int:
    property_id = insert_candidate(
        conn,
        Candidate(
            source="wowhaus", source_rank="A", source_url=url,
            title=extra.pop("title", "A house"), content_text="...",
            is_for_sale=1, location_country=country,
            location_city=extra.pop("city", "San Francisco"),
        ),
    )
    if score is not None:
        conn.execute(
            "UPDATE properties SET score = ?, genre = ?, scored_at = ? WHERE id = ?",
            (score, genre, "2026-09-15T00:00:00+00:00", property_id),
        )
    for key, value in extra.items():
        conn.execute(f"UPDATE properties SET {key} = ? WHERE id = ?", (value, property_id))
    conn.commit()
    return property_id


def _post(conn, post_id, property_id, published_at, *, reach=None, note=None, kind="feed"):
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, published_at, property_id, "
        "reach, note) VALUES (?, ?, 'published', ?, ?, ?, ?, ?)",
        (post_id, kind, published_at, published_at, property_id, reach, note),
    )
    conn.commit()


def test_週は月曜から日曜(config) -> None:
    start, end = week_bounds(config, NOW)
    assert start.strftime("%Y-%m-%d") == "2026-09-14"   # 月曜
    assert end.strftime("%Y-%m-%d") == "2026-09-21"


def test_今週出したものが主役(config, conn) -> None:
    """未審査の候補は並べない（未審査タブの仕事）。"""
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect",
             display_name="Steel Ranch")
    b = _add(conn, "https://a.example.com/2", score=60, genre="loft",
             display_name="先週の1軒")
    _add(conn, "https://a.example.com/3", score=95, genre="hidden_gem",
         display_name="未審査の高得点")      # 出していない。レポートに出さない
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=131)
    _post(conn, 2, b, "2026-09-08T00:02:00+00:00", reach=300)   # 先週

    report = build(config, conn, NOW)
    assert [row["id"] for row in report.published] == [1]
    assert "未審査の高得点" not in render(report)


def test_未取得を0として平均に混ぜない(config, conn) -> None:
    """出したばかりの投稿はまだ集計されていない。0 扱いすると平均が下がる。"""
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    b = _add(conn, "https://a.example.com/2", score=70, genre="loft")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=200)
    _post(conn, 2, b, "2026-09-16T00:02:00+00:00")              # 未取得

    report = build(config, conn, NOW)
    assert report.reach_total == 200
    assert report.reach_avg == 200.0      # 100 ではない
    assert report.measured == 1
    check = next(c for c in report.checks if "リーチが全部読めている" in c.label)
    assert check.ok is False


def test_先週と比べる(config, conn) -> None:
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    b = _add(conn, "https://a.example.com/2", score=70, genre="loft")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=150)
    _post(conn, 2, b, "2026-09-08T00:02:00+00:00", reach=100)

    report = build(config, conn, NOW)
    assert report.prev_count == 1
    assert report.prev_total == 100
    assert report.total_delta == 50


def test_いちばん見られた1本(config, conn) -> None:
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect",
             display_name="低い方")
    b = _add(conn, "https://a.example.com/2", score=70, genre="loft",
             display_name="高い方")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=100)
    _post(conn, 2, b, "2026-09-16T00:02:00+00:00", reach=300)
    assert build(config, conn, NOW).best["id"] == 2


def test_カルテの抜粋が出る(config, conn) -> None:
    """**メモではなく、カルテの中身**を抜く。何を評価して出したのか。"""
    import json

    a = _add(conn, "https://a.example.com/1", score=88, genre="architect",
             architect="Harold B. Zook", year_built="1938")
    conn.execute(
        "UPDATE properties SET score_detail = ?, style_identified = 1, "
        "one_of_a_kind = 1 WHERE id = ?",
        (json.dumps({"gate": "", "axes": [
            {"key": "source", "raw": 80.0, "weight": 0.1, "reason": "ソースA"},
            {"key": "story", "raw": 90.0, "weight": 0.25, "reason": "設計者が特定できる"},
        ]}, ensure_ascii=False), a),
    )
    conn.commit()
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=100, note="1枚目が弱い")

    text = render(build(config, conn, NOW))
    assert "88点" in text
    assert "様式の特定" in text and "一点物" in text
    assert "Harold B. Zook" in text
    assert "設計者が特定できる" in text      # story 軸の理由
    assert "1枚目が弱い" not in text          # **メモは出さない**


def test_判定はstory軸から抜く(config, conn) -> None:
    """他の軸は機械的に決まるので、振り返りの材料にならない。"""
    import json

    from freming.report.weekly import judgement

    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    conn.execute(
        "UPDATE properties SET score_detail = ? WHERE id = ?",
        (json.dumps({"axes": [
            {"key": "genre", "raw": 100.0, "reason": "architect"},
            {"key": "story", "raw": 90.0, "reason": "1938年築のチューダー様式"},
        ]}, ensure_ascii=False), a),
    )
    conn.commit()
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=100)
    row = build(config, conn, NOW).published[0]
    assert judgement(row) == "1938年築のチューダー様式"


def test_出したものの偏りを見る(config, conn) -> None:
    """**候補ではなく、出したもの**について確かめる。"""
    for i in range(3):
        pid = _add(conn, f"https://a.example.com/{i}", score=70, genre="architect",
                   country="United States")
        _post(conn, i + 1, pid, f"2026-09-1{5 + i}T00:02:00+00:00", reach=100)

    report = build(config, conn, NOW)
    assert next(c for c in report.checks if "米国" in c.label).ok is False
    assert next(c for c in report.checks if "ジャンル" in c.label).ok is False


def test_ジャンル別の平均は直近数週で見る(config, conn) -> None:
    """1週間では1ジャンル1本になる。数週ためないと傾向にならない。"""
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    b = _add(conn, "https://a.example.com/2", score=70, genre="loft")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=300)
    _post(conn, 2, b, "2026-08-20T00:02:00+00:00", reach=100)   # 数週前でも入る

    stats = {s.genre: s for s in build(config, conn, NOW).genres}
    assert stats["architect"].reach_avg == 300
    assert stats["loft"].reach_avg == 100
    # 高い順に並べる
    assert build(config, conn, NOW).genres[0].genre == "architect"


def test_在庫は件数だけ(config, conn) -> None:
    """未審査は並べず、数とリンクだけ置く。"""
    _add(conn, "https://a.example.com/1", score=95, genre="hidden_gem",
         display_name="未審査の1軒")
    report = build(config, conn, NOW)
    assert report.pending == 1
    assert "未審査の1軒" not in render(report)


def test_テキストでも読める(config, conn) -> None:
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect",
             display_name="Grayoaks")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=131)
    text = render(build(config, conn, NOW))
    assert "WEEKLY REPORT" in text
    assert "Grayoaks" in text
    assert "いちばん見られた1本" in text


def test_画面が開く(config, conn) -> None:
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect",
             display_name="Grayoaks")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=131, note="1枚目が弱い")
    body = TestClient(create_app(config)).get("/report").text
    assert "WEEKLY REPORT" in body
    assert "Grayoaks" in body
    assert "カルテを開く" in body
    assert "1枚目が弱い" not in body        # メモはレポートに出さない


def test_何も出していない週でも開く(config, conn) -> None:
    client = TestClient(create_app(config))
    assert client.get("/report?week=2026-08-03").status_code == 200


def test_壊れた週の指定でも画面は開く(config, conn) -> None:
    client = TestClient(create_app(config))
    assert client.get("/report?week=まいにち").status_code == 200


def test_画面に出る文字に星印を書かない(config, conn) -> None:
    """`**強調**` は Markdown。HTMLではそのまま星印が出る。

    2026-09-16 の実機で「**埋めるために弱い案件を入れない**」と表示されて
    いた。コード中のコメントと、画面に出る文字を混同しない。
    """
    _posted(conn, 4, genre="adaptive_reuse", reach=400, url_seed=1)
    _posted(conn, 5, genre="architect", reach=100, images=3, url_seed=2)
    report = build(config, conn, NOW)
    for check in report.checks:
        assert "**" not in check.label
        assert "**" not in check.note
    # 考察も同じ。画面に出る文字はすべて対象
    for insight in report.insights:
        assert "**" not in insight.headline
        assert "**" not in insight.evidence
        assert "**" not in insight.suggestion


# --- 投稿カルテ -------------------------------------------------------

def test_カルテに採点とリーチと写真が出る(config, conn) -> None:
    """1投稿について分かることを1画面に集める。"""
    import json

    property_id = _add(conn, "https://a.example.com/1", score=82, genre="architect",
                       display_name="Hezlep House", architect="Harold B. Zook")
    conn.execute(
        "UPDATE properties SET score_detail = ?, style_identified = 1 WHERE id = ?",
        (json.dumps({"gate": "", "axes": [
            {"key": "story", "raw": 90.0, "weight": 0.25, "reason": "設計者が特定できる"},
        ], "flags": {}}, ensure_ascii=False), property_id),
    )
    conn.execute(
        "INSERT INTO images (property_id, source_url, position, origin_url) "
        "VALUES (?, 'https://cdn.example.com/1.jpg', 1, NULL)", (property_id,),
    )
    conn.execute(
        "INSERT INTO images (property_id, source_url, position, origin_url) "
        "VALUES (?, 'https://cdn.other.org/2.jpg', 2, 'https://other.org/article')",
        (property_id,),
    )
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, published_at, property_id, "
        "reach, reach_checked_at, caption) VALUES (1, 'feed', 'published', ?, ?, ?, 384, ?, ?)",
        ("2026-09-15T00:02:00+00:00", "2026-09-15T00:02:00+00:00", property_id,
         "2026-09-16T00:00:00+00:00", "【 Hezlep House 】"),
    )
    conn.commit()

    body = TestClient(create_app(config)).get("/posts/1").text
    assert "Hezlep House" in body
    assert "384" in body                      # リーチ
    assert "設計者が特定できる" in body        # 採点の軸と理由
    assert "https://other.org/article" in body  # 他サイトから足した写真の出所


def test_リーチ未取得と0を混ぜない(config, conn) -> None:
    property_id = _add(conn, "https://a.example.com/1", score=60, genre="loft")
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, property_id) "
        "VALUES (1, 'feed', 'planned', ?, ?)",
        ("2026-09-18T00:02:00+00:00", property_id),
    )
    conn.commit()
    body = TestClient(create_app(config)).get("/posts/1").text
    assert "未取得" in body


def test_メモは保存されて公開されない(config, conn) -> None:
    property_id = _add(conn, "https://a.example.com/1", score=60, genre="loft")
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, property_id, caption) "
        "VALUES (1, 'feed', 'planned', ?, ?, '本文')",
        ("2026-09-18T00:02:00+00:00", property_id),
    )
    conn.commit()
    client = TestClient(create_app(config))
    client.post("/posts/1/note", data={"note": "1枚目が弱い"})

    row = conn.execute("SELECT note, note_at, caption FROM posts WHERE id = 1").fetchone()
    assert row["note"] == "1枚目が弱い"
    assert row["note_at"]
    assert row["caption"] == "本文"      # **本文は書き換えない**


def test_空のメモは消える(config, conn) -> None:
    property_id = _add(conn, "https://a.example.com/1", score=60, genre="loft")
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, property_id, note) "
        "VALUES (1, 'feed', 'planned', ?, ?, '前のメモ')",
        ("2026-09-18T00:02:00+00:00", property_id),
    )
    conn.commit()
    TestClient(create_app(config)).post("/posts/1/note", data={"note": "   "})
    assert conn.execute("SELECT note FROM posts WHERE id = 1").fetchone()["note"] is None


def test_無い投稿は404(config) -> None:
    assert TestClient(create_app(config)).get("/posts/999").status_code == 404


def test_カルテに経緯が出る(config, conn) -> None:
    """収集 → 採点 → 審査 → 納品 → 公開 の足取りを1画面に。"""
    property_id = _add(conn, "https://a.example.com/1", score=82, genre="architect",
                       display_name="Hezlep House")
    conn.execute(
        "UPDATE properties SET collected_at = ?, scored_at = ?, reviewed_at = ?, "
        "status = 'delivered', score_model = 'claude-haiku-4-5', "
        "usage_type = '住宅', structure = '木造', building_area = '210 m2', "
        "site_area = '1,200 m2', photo_credit = 'Studio X', "
        "summary = '設計者の自邸。オリジナルの木工が残る。' WHERE id = ?",
        ("2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00",
         "2026-09-03T00:00:00+00:00", property_id),
    )
    conn.execute(
        "INSERT INTO deliveries (property_id, folder_name, image_count, delivered_at) "
        "VALUES (?, 'frmg_ig012', 10, '2026-09-04T00:00:00+00:00')", (property_id,),
    )
    _post(conn, 1, property_id, "2026-09-15T00:02:00+00:00", reach=300)
    conn.execute(
        "UPDATE posts SET ig_media_id = '18140889871599903' WHERE id = 1"
    )
    conn.commit()

    body = TestClient(create_app(config)).get("/posts/1").text
    assert "経緯" in body
    assert "claude-haiku-4-5" in body        # 採点に使ったモデル
    assert "frmg_ig012" in body              # 納品フォルダ
    assert "18140889871599903" in body       # media_id
    assert "木造" in body and "210 m2" in body  # 仕様
    assert "Studio X" in body                # 撮影者
    assert "設計者の自邸" in body             # 説明


def test_カルテは平均と比べる(config, conn) -> None:
    """数字だけでは高いのか低いのか分からない。"""
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    b = _add(conn, "https://a.example.com/2", score=70, genre="loft")
    _post(conn, 1, a, "2026-09-15T00:02:00+00:00", reach=300)
    _post(conn, 2, b, "2026-09-10T00:02:00+00:00", reach=100)

    body = TestClient(create_app(config)).get("/posts/1").text
    assert "平均との差" in body


def test_失敗した投稿は理由まで出す(config, conn) -> None:
    a = _add(conn, "https://a.example.com/1", score=70, genre="architect")
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, property_id, attempts, error) "
        "VALUES (1, 'feed', 'failed', ?, ?, 3, 'コンテナが ERROR になりました')",
        ("2026-09-15T00:02:00+00:00", a),
    )
    conn.commit()
    body = TestClient(create_app(config)).get("/posts/1").text
    assert "3回" in body
    assert "コンテナが ERROR" in body


# --- 考察 -------------------------------------------------------------

def _posted(conn, n, *, genre, reach, images=10, style=0, one=0, day=1, url_seed=0):
    """通常投稿を n 本作る（考察の材料）。"""
    for i in range(n):
        pid = _add(conn, f"https://a.example.com/t{url_seed}-{genre}-{i}",
                   score=70, genre=genre)
        # 出した物件は納品済み。**未審査の在庫には数えない**（本番と同じ）
        conn.execute(
            "UPDATE properties SET style_identified = ?, one_of_a_kind = ?, "
            "status = 'delivered' WHERE id = ?",
            (style, one, pid),
        )
        for pos in range(images):
            conn.execute(
                "INSERT INTO images (property_id, source_url, position) VALUES (?, ?, ?)",
                (pid, f"https://cdn.example.com/{pid}-{pos}.jpg", pos + 1),
            )
        post_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM posts").fetchone()["n"]
        _post(conn, post_id, pid, f"2026-09-0{day}T00:02:00+00:00", reach=reach)
    conn.commit()


def test_本数が足りないうちは何も言わない(config, conn) -> None:
    """**1本の当たり外れを法則にしない。** これが考察の肝。"""
    _posted(conn, 3, genre="architect", reach=300)
    insights = build(config, conn, NOW).insights
    assert len(insights) == 1
    assert "まだ何も言えません" in insights[0].headline
    assert "3 本" in insights[0].evidence


def test_伸びているジャンルと在庫を出す(config, conn) -> None:
    _posted(conn, 4, genre="adaptive_reuse", reach=400, url_seed=1)
    _posted(conn, 5, genre="architect", reach=100, url_seed=2)
    # 未審査の在庫（提案の材料）
    _add(conn, "https://a.example.com/stock", score=80, genre="adaptive_reuse")

    insights = build(config, conn, NOW).insights
    top = next(i for i in insights if "Conversion" in i.headline)
    assert "400" in top.evidence            # その群の平均
    assert "1 件" in top.suggestion         # 在庫の件数


def test_在庫が無ければそう言う(config, conn) -> None:
    _posted(conn, 4, genre="adaptive_reuse", reach=400, url_seed=1)
    _posted(conn, 5, genre="architect", reach=100, url_seed=2)
    top = next(i for i in build(config, conn, NOW).insights if "Conversion" in i.headline)
    assert "在庫がありません" in top.suggestion


def test_差が小さくても黙らない(config, conn) -> None:
    """**毎週かならず何か出す。** 以前は「はっきりした差はありません」の
    1行で終わっていたが、この規模では毎週それになり、欄として死んでいた
    （2026-09-17 の指摘）。差が小さいことは札で示す。
    """
    _posted(conn, 5, genre="architect", reach=200, url_seed=1)
    _posted(conn, 5, genre="loft", reach=205, url_seed=2)
    insights = build(config, conn, NOW).insights
    assert insights, "何も出さないのは禁止"
    assert any("どれを出しても同じくらい" in i.headline for i in insights)
    # 差が小さいので「傾向」は名乗らない
    assert all(i.strength != "傾向" for i in insights)


def test_小さい差も仮説として出す(config, conn) -> None:
    """**少しの差でも、向きが出ていれば言う。** 確度は札で示す。"""
    _posted(conn, 4, genre="architect", reach=220, style=1, url_seed=1)
    _posted(conn, 4, genre="architect", reach=200, style=0, url_seed=2)
    insight = next(i for i in build(config, conn, NOW).insights
                   if "様式の特定" in i.headline)
    assert insight.strength == "仮説"          # 15%未満なので「傾向」ではない
    assert "差 10%" in insight.evidence
    assert "1本の当たり外れで消える" in insight.evidence
    assert insight.suggestion                  # 次の一手は必ず添える


def test_差の大きい順に並ぶ(config, conn) -> None:
    _posted(conn, 4, genre="architect", reach=400, style=1, url_seed=1)
    _posted(conn, 4, genre="loft", reach=100, style=0, url_seed=2)
    insights = build(config, conn, NOW).insights
    lifts = [i.lift for i in insights]
    assert lifts == sorted(lifts, reverse=True)
    assert len(insights) <= 3


def test_様式の特定がリーチでも効いていれば言う(config, conn) -> None:
    _posted(conn, 4, genre="architect", reach=400, style=1, url_seed=1)
    _posted(conn, 4, genre="architect", reach=100, style=0, url_seed=2)
    insight = next(i for i in build(config, conn, NOW).insights
                   if "様式の特定" in i.headline)
    assert "あり 400" in insight.evidence


def test_効いていないときも同じ強さで書く(config, conn) -> None:
    """**都合の良い方だけ出さない。** 逆向きでも同じように出す。"""
    _posted(conn, 4, genre="architect", reach=100, one=1, url_seed=1)
    _posted(conn, 4, genre="architect", reach=400, one=0, url_seed=2)
    insight = next(i for i in build(config, conn, NOW).insights
                   if "一点物" in i.headline)
    assert "効いていません" in insight.headline
    assert "すぐには変えません" in insight.suggestion


def test_考察は画面にも出る(config, conn) -> None:
    _posted(conn, 4, genre="adaptive_reuse", reach=400, url_seed=1)
    _posted(conn, 5, genre="architect", reach=100, url_seed=2)
    body = TestClient(create_app(config)).get("/report").text
    assert "考察" in body
    assert "根拠:" in body
    assert "平均の差だけ" in body        # 因果ではないと断ってある
