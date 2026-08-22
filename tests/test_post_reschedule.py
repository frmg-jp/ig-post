"""`freming post reschedule` の検証。

ワーカーを止めている間も予定は毎朝作られる。動かした瞬間にまとめて出る
のを避けるための経路なので、**動かす対象の選び方**を固定しておく。

1日3投稿から1投稿へ減らしたときも同じ問題が起きる。既にある予定は古い枠
のまま残るので、枠から外れた行も動かす対象に入れている。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

from freming.cli import main
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import create_post, finish_post

JST = ZoneInfo("Asia/Tokyo")


def _config(tmp_path, post_times: list[str]) -> str:
    cfg = yaml.safe_load(open("config.yaml"))
    cfg["app"]["db_path"] = str(tmp_path / "test.db")
    cfg["instagram"]["post_times"] = post_times
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
    return str(path)


def _at(days: int, hhmm: str) -> datetime:
    hour, _, minute = hhmm.partition(":")
    day = (datetime.now(UTC).astimezone(JST) + timedelta(days=days)).date()
    moment = datetime.combine(day, datetime.min.time(), tzinfo=JST)
    return moment.replace(hour=int(hour), minute=int(minute)).astimezone(UTC)


def _db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    return connect(path)


def _post(conn, when: datetime, title: str) -> int:
    cursor = conn.execute(
        "INSERT INTO properties (source, source_url, title, location_city, "
        "location_country, summary, score, status, collected_at) "
        "VALUES ('dezeen', ?, ?, 'Porto', 'Portugal', 's', 80, 'delivered', ?) "
        "RETURNING id",
        (f"https://example.com/{title}", title, datetime.now(UTC).isoformat()),
    )
    property_id = cursor.fetchone()["id"]
    conn.commit()
    return create_post(conn, "feed", when.isoformat(), property_id=property_id, caption="c")


def _times(conn) -> list[str]:
    rows = conn.execute(
        "SELECT scheduled_at FROM posts ORDER BY scheduled_at"
    ).fetchall()
    return [
        datetime.fromisoformat(row["scheduled_at"]).astimezone(JST).strftime("%H:%M")
        for row in rows
    ]


def test_過ぎた予定は先の空き枠へ動く(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["20:00"])
    conn = _db(tmp_path)
    _post(conn, _at(-2, "20:00"), "Old")
    conn.close()

    assert main(["--config", config, "post", "reschedule"]) == 0

    conn = connect(tmp_path / "test.db")
    assert _times(conn) == ["20:00"]
    moved = conn.execute("SELECT scheduled_at FROM posts").fetchone()["scheduled_at"]
    assert moved > datetime.now(UTC).isoformat()


def test_枠から外れた予定も動く(monkeypatch, tmp_path) -> None:
    """1日3投稿から1投稿に減らしたとき、古い枠の行が残らないこと。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["20:00"])
    conn = _db(tmp_path)
    _post(conn, _at(1, "09:00"), "Morning")   # もう使わない枠
    _post(conn, _at(1, "20:00"), "Evening")   # いまの枠。動かさない
    conn.close()

    assert main(["--config", config, "post", "reschedule"]) == 0

    conn = connect(tmp_path / "test.db")
    assert _times(conn) == ["20:00", "20:00"]


def test_出したあとの投稿は動かさない(monkeypatch, tmp_path) -> None:
    """**投稿済みを動かすと、同じものがもう一度出る。**"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["20:00"])
    conn = _db(tmp_path)
    post_id = _post(conn, _at(-2, "09:00"), "Published")
    finish_post(conn, post_id, "media-1", "c")
    before = conn.execute(
        "SELECT scheduled_at FROM posts WHERE id = ?", (post_id,)
    ).fetchone()["scheduled_at"]
    conn.close()

    assert main(["--config", config, "post", "reschedule"]) == 0

    conn = connect(tmp_path / "test.db")
    after = conn.execute(
        "SELECT scheduled_at FROM posts WHERE id = ?", (post_id,)
    ).fetchone()["scheduled_at"]
    assert after == before


def test_動かすものが無ければ何もしない(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["20:00"])
    conn = _db(tmp_path)
    _post(conn, _at(1, "20:00"), "Fine")
    conn.close()

    assert main(["--config", config, "post", "reschedule"]) == 0
    assert "動かす予定はありません" in capsys.readouterr().out


def test_CLIから見送りと取り消しができる(monkeypatch, tmp_path, capsys) -> None:
    """審査UIが開けない状況でも、run の前に順番を整えられること。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["20:00"])
    conn = _db(tmp_path)
    post_id = _post(conn, _at(-1, "20:00"), "Skippable")
    conn.close()

    assert main(["--config", config, "post", "skip", "--id", str(post_id)]) == 0
    conn = connect(tmp_path / "test.db")
    assert conn.execute("SELECT state FROM posts WHERE id=?",
                        (post_id,)).fetchone()["state"] == "skipped"
    conn.close()

    assert main(["--config", config, "post", "unskip", "--id", str(post_id)]) == 0
    conn = connect(tmp_path / "test.db")
    assert conn.execute("SELECT state FROM posts WHERE id=?",
                        (post_id,)).fetchone()["state"] == "planned"


def test_見送りはIDが無ければ何もしない(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["20:00"])
    _db(tmp_path).close()
    assert main(["--config", config, "post", "skip"]) == 2
