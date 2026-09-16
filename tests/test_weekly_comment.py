"""[10] 週次レポートの講評（LLM）。

**危険は1つだけ。数字に無い話を書くこと。** 渡していない事実を作られると、
次の選定が嘘を根拠に動く。言い回しの縛りだけでは止まらないので、出てきた
数字を検算して、合わなければ捨てる。

APIは呼ばない（返答を差し替える）。確かめるのは検算と保存。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from freming.collect.base import Candidate
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import insert_candidate
from freming.report import comment
from freming.report.weekly import build
from freming.web.app import create_app
from tests.test_weekly_report import NOW


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "comment.db"
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


def _week(conn) -> None:
    """今週1本出した状態を作る。"""
    property_id = insert_candidate(
        conn,
        Candidate(
            source="wowhaus", source_rank="A", source_url="https://a.example.com/1",
            title="Steel Ranch", content_text="...", is_for_sale=1,
            location_country="United States", location_city="Ross",
        ),
    )
    conn.execute(
        "UPDATE properties SET score = 82, genre = 'architect', "
        "display_name = 'Steel Ranch', status = 'delivered' WHERE id = ?",
        (property_id,),
    )
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, published_at, property_id, reach) "
        "VALUES (1, 'feed', 'published', ?, ?, ?, 384)",
        ("2026-09-15T00:02:00+00:00", "2026-09-15T00:02:00+00:00", property_id),
    )
    conn.commit()


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


def _fake_anthropic(monkeypatch, *texts: str):
    """Anthropic クライアントを差し替える。**APIは呼ばない。**

    2つ以上渡すと、呼ばれた順に返す（書き直しの確認用）。最後のものは
    それ以降ずっと返る。
    """
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    seen: list[list[dict]] = []

    class _Messages:
        def create(self, **kwargs):
            seen.append(kwargs["messages"])
            return _Response(texts[min(len(seen) - 1, len(texts) - 1)])

    class _Client:
        def __init__(self, **_kwargs) -> None:
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    return seen


# --- 材料 -------------------------------------------------------------

def test_渡すのは数字だけ(config, conn) -> None:
    """記事の本文や画像は渡さない。**数字に無い話を書かせないため。**"""
    _week(conn)
    source = comment.build_source(build(config, conn, NOW))
    assert "384" in source
    assert "Steel Ranch" in source
    assert "content_text" not in source
    assert "http" not in source        # URLも渡さない


# --- 検算 -------------------------------------------------------------

def test_渡していない数字を見つける() -> None:
    source = "出した本数: 3\nリーチ合計: 335"
    assert comment.unsupported_numbers("3本で335でした", source) == set()
    # 勝手な計算（先週比 42% など）は材料に無い
    assert comment.unsupported_numbers("先週より42%伸びました", source) == {"42"}


def test_単位の無い1桁は見逃す() -> None:
    """「3文で」「1つだけ」のような言い回しにも数字が出る。"""
    assert comment.unsupported_numbers("次に試すことを1つ", "本数: 12") == set()


def test_単位の付いた1桁は見る() -> None:
    """**「7〜8本」の 7 を止める。** 2026-09-16 にこれがすり抜けた。"""
    source = "出した本数: 8"
    assert comment.unsupported_numbers("先週並みの8本に戻す", source) == set()
    assert comment.unsupported_numbers("7〜8本に戻す", source) == {"7"}
    assert comment.unsupported_numbers("写真を3枚足す", source) == {"3"}


def test_数字が合わない講評は保存しない(config, conn, monkeypatch) -> None:
    """**黙って直さない。** 捨てて、理由を出す。"""
    _week(conn)
    seen = _fake_anthropic(monkeypatch, "今週はリーチ 9999 でした。")
    with pytest.raises(RuntimeError, match="9999"):
        comment.write(config, build(config, conn, NOW))
    assert len(seen) == 2, "1回だけ書き直させる"


def test_書き直しで直れば保存する(config, conn, monkeypatch) -> None:
    """初回に落ちるのは、たいてい丸め（87→「90近く」）。書き直せば通る。

    **検算を緩めるわけではない。** 直したものも同じ検算にかけている。
    """
    _week(conn)
    seen = _fake_anthropic(
        monkeypatch,
        "リーチは390近くでした。",          # 390 は材料に無い（丸め）
        "リーチは384でした。次に試すこと: 写真を増やす。",
    )
    written = comment.write(config, build(config, conn, NOW))
    assert "384" in written.body
    assert len(seen) == 2
    # 書き直しの指示では、落ちた数字を名指しする
    assert "390" in seen[1][-1]["content"]
    assert seen[1][1]["role"] == "assistant"   # 前の文も渡している


def test_空の返答も保存しない(config, conn, monkeypatch) -> None:
    _week(conn)
    _fake_anthropic(monkeypatch, "   ")
    with pytest.raises(RuntimeError, match="空"):
        comment.write(config, build(config, conn, NOW))


# --- 保存と表示 -------------------------------------------------------

def test_書いて保存して画面に出る(config, conn, monkeypatch) -> None:
    _week(conn)
    _fake_anthropic(
        monkeypatch,
        "今週は1本を出し、リーチは384でした。本数が少ないので傾向は言えません。"
        "次に試すこと: 写真の枚数が少ない物件を先に埋める。",
    )
    report = build(config, conn, NOW)
    written = comment.write(config, report)
    comment.save(conn, report.start.date().isoformat(), written)

    body = TestClient(create_app(config)).get("/report").text
    assert "講評" in body
    assert "リーチは384でした" in body
    assert "材料に Claude が書いています" in body   # 出どころを明示


def test_先週の講評が今週の画面に出る(config, conn, monkeypatch) -> None:
    """**講評は終わった週について書く**ので、今週にはまだ無い。

    空欄を出すより、先週書いたものを週の名前つきで出す。どの週の話かを
    書かないと「今週の講評」と読まれる。
    """
    _week(conn)
    report = build(config, conn, NOW)
    last_week = (report.start - timedelta(days=7)).date().isoformat()
    comment.save(
        conn, last_week,
        comment.Comment(body="先週は静かな週でした。", source="本数: 1", model="m"),
    )

    body = TestClient(create_app(config)).get("/report").text
    assert "先週は静かな週でした" in body
    assert last_week.replace("-", "/") in body      # どの週のものか
    assert "まだ途中なので" in body


def test_未来の週の講評は出さない(config, conn) -> None:
    """過去の週を ?week= で見ているときに、その後に書いた講評は出さない。"""
    _week(conn)
    report = build(config, conn, NOW)
    comment.save(
        conn, report.start.date().isoformat(),
        comment.Comment(body="今週の講評です。", source="本数: 1", model="m"),
    )
    old = (report.start - timedelta(days=21)).date().isoformat()

    body = TestClient(create_app(config)).get(f"/report?week={old}").text
    assert "今週の講評です" not in body


def test_last_week_は1週前を出す(config, conn, monkeypatch) -> None:
    """月曜の朝に走るので、--last-week が無いと0本の週を講評してしまう。"""
    _week(conn)
    import freming.cli as cli
    from freming.cli import main
    monkeypatch.setattr(cli, "load_config", lambda *_a, **_k: config)
    _fake_anthropic(monkeypatch, "静かな週でした。次に試すこと: 在庫を増やす。")

    # --week 2026-09-14 の1週前 = 09/07 の週（終わっている）に書く。
    assert main(["report", "--week", "2026-09-14", "--last-week", "--comment"]) == 0
    assert comment.load(conn, "2026-09-07") is not None
    assert comment.load(conn, "2026-09-14") is None


def test_途中の週には書かせない(config, conn, monkeypatch) -> None:
    """**「半分に落ち込んだ」と読まれる。** 2026-09-16 に実際に出た。"""
    _week(conn)
    import freming.cli as cli
    from freming.cli import main
    monkeypatch.setattr(cli, "load_config", lambda *_a, **_k: config)
    seen = _fake_anthropic(monkeypatch, "今週は1本でした。")

    assert main(["report", "--comment"]) == 2
    assert seen == [], "APIを呼ばずに止める"


def test_講評を消せる(config, conn, monkeypatch) -> None:
    """読み間違いを書いたものを残さない。"""
    _week(conn)
    report = build(config, conn, NOW)
    week = report.start.date().isoformat()
    comment.save(conn, week,
                 comment.Comment(body="消す対象。", source="本数: 1", model="m"))

    import freming.cli as cli
    from freming.cli import main
    monkeypatch.setattr(cli, "load_config", lambda *_a, **_k: config)
    assert main(["report", "--week", week, "--clear-comment"]) == 0
    assert comment.load(conn, week) is None


def test_講評が無くても画面は開く(config, conn) -> None:
    """**画面からはAPIを呼ばない。** 無ければ出さないだけ。"""
    _week(conn)
    body = TestClient(create_app(config)).get("/report").text
    assert body.count("<h2>講評</h2>") == 0
    assert "WEEKLY REPORT" in body


def test_同じ週に二度書かない(config, conn, monkeypatch) -> None:
    _week(conn)
    _fake_anthropic(monkeypatch, "今週は1本でした。")
    report = build(config, conn, NOW)
    week = report.start.date().isoformat()
    comment.save(conn, week, comment.write(config, report))

    import freming.cli as cli
    from freming.cli import main
    monkeypatch.setattr(cli, "load_config", lambda *_a, **_k: config)

    calls = {"n": 0}
    original = comment.write

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(comment, "write", _counting)
    assert main(["report", "--week", "2026-09-16", "--comment"]) == 0
    assert calls["n"] == 0        # 既にあるので呼ばない
