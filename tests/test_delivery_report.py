"""承認済みで納品できていない物件の一覧（delivery-report）。読むだけ。"""

from __future__ import annotations

import pytest

from freming import cli
from freming.collect.base import Candidate
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import insert_candidate, record_delivery_failure


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "report.db"
    migrate(path)
    conn = connect(path)
    yield conn
    conn.close()


def _config_for(conn):
    from freming.config import load_config

    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = conn.execute("PRAGMA database_list").fetchall()[0]["file"]
    return cfg


def _approved(conn, source: str, url: str) -> int:
    pid = insert_candidate(conn, Candidate(
        source=source, source_rank="A", source_url=url, title=f"{source} house",
        content_text="...", is_for_sale=1,
    ))
    conn.execute("UPDATE properties SET status = 'approved', score = 80 WHERE id = ?", (pid,))
    conn.commit()
    return pid


def test_理由をソースごとに数えて出す(db, monkeypatch, capsys):
    stuck = _approved(db, "robbreport_shelter", "https://example.com/a")
    for _ in range(3):
        record_delivery_failure(db, stuck, "画像なし: 加工できた画像がありません")
    _approved(db, "vanguard", "https://example.com/b")   # 未試行
    monkeypatch.setattr(cli, "load_config", lambda *_a, **_k: _config_for(db))

    assert cli.main(["delivery-report"]) == 0
    out = capsys.readouterr().out
    assert "承認済み・未納品 2 件（うち 1 件は 3 回失敗" in out
    assert "理由: 画像なし: 加工できた画像がありません" in out
    assert "1  robbreport_shelter / 画像なし" in out
    assert "1  vanguard / 未試行" in out


def test_無ければそう言う(db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda *_a, **_k: _config_for(db))
    assert cli.main(["delivery-report"]) == 0
    assert "ありません" in capsys.readouterr().out
