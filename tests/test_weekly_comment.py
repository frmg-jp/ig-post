"""[10] 週次レポートの講評（LLM）。

**危険は1つだけ。数字に無い話を書くこと。** 渡していない事実を作られると、
次の選定が嘘を根拠に動く。言い回しの縛りだけでは止まらないので、出てきた
数字を検算して、合わなければ捨てる。

APIは呼ばない（返答を差し替える）。確かめるのは検算と保存。
"""

from __future__ import annotations

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


def _fake_anthropic(monkeypatch, text: str):
    """Anthropic クライアントを差し替える。**APIは呼ばない。**"""
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class _Messages:
        def create(self, **_kwargs):
            return _Response(text)

    class _Client:
        def __init__(self, **_kwargs) -> None:
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)


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


def test_1桁は見逃す() -> None:
    """「3文で」「1つだけ」のような言い回しにも数字が出る。"""
    assert comment.unsupported_numbers("次に試すことを1つ", "本数: 12") == set()


def test_数字が合わない講評は保存しない(config, conn, monkeypatch) -> None:
    """**黙って直さない。** 捨てて、理由を出す。"""
    _week(conn)
    _fake_anthropic(monkeypatch, "今週はリーチ 9999 でした。")
    with pytest.raises(RuntimeError, match="9999"):
        comment.write(config, build(config, conn, NOW))


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
