"""`freming post reschedule` の検証。

ワーカーを止めている間も予定は毎朝作られる。動かした瞬間にまとめて出る
のを避けるための経路なので、**動かす対象の選び方**を固定しておく。

1日3投稿から1投稿へ減らしたときも同じ問題が起きる。既にある予定は古い枠
のまま残るので、枠から外れた行も動かす対象に入れている。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
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


def _slots(config_path: str, count: int) -> list[datetime]:
    """これから使われる枠を、**本番と同じ決め方**で取る。

    「明日の9時」と決め打ちにしない。slot_times は過ぎた枠を返さないので、
    09:00 より前に走らせるとその日の枠がまだ生きていて1日ぶんずれる。
    日付で固定すると、**毎日 0:00〜9:00 の間だけ落ちるテスト**になる
    （実際にそうなっていた。2026-08-31 に発覚）。
    """
    from freming.config import load_config
    from freming.instagram.plan import slot_times

    return slot_times(load_config(config_path), datetime.now(UTC))[:count]


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


def test_出し直しは次の空き枠に置かれる(monkeypatch, tmp_path, capsys) -> None:
    """auto_post が動いている間に「今」へ戻すと、時刻を直す暇なく出てしまう。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["09:00"])
    conn = _db(tmp_path)
    post_id = _post(conn, _at(-1, "09:00"), "Republish")
    from freming.db.repository import claim_due_post

    claim_due_post(conn, datetime.now(UTC).isoformat(), 3)
    finish_post(conn, post_id, "media-1", "c1")
    conn.close()

    assert main(["--config", config, "post", "requeue", "--id", str(post_id)]) == 0

    conn = connect(tmp_path / "test.db")
    row = conn.execute("SELECT state, scheduled_at FROM posts WHERE id=?",
                       (post_id,)).fetchone()
    assert row["state"] == "planned"
    assert row["scheduled_at"] > datetime.now(UTC).isoformat()
    local = datetime.fromisoformat(row["scheduled_at"]).astimezone(JST)
    assert local.strftime("%H:%M") == "09:00"


def test_出し直しはnowで今すぐに戻せる(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["09:00"])
    conn = _db(tmp_path)
    post_id = _post(conn, _at(-1, "09:00"), "RepublishNow")
    from freming.db.repository import claim_due_post

    claim_due_post(conn, datetime.now(UTC).isoformat(), 3)
    finish_post(conn, post_id, "media-1", "c1")
    conn.close()

    assert main(["--config", config, "post", "requeue", "--id", str(post_id),
                 "--now"]) == 0
    conn = connect(tmp_path / "test.db")
    row = conn.execute("SELECT scheduled_at FROM posts WHERE id=?", (post_id,)).fetchone()
    assert row["scheduled_at"] <= datetime.now(UTC).isoformat()


# 枠の時刻を2つで試す。**いつ走らせても、片方は「その日の枠がもう過ぎて
# いる」側、もう片方は「まだ来ていない」側になる。** 日付で決め打ちして
# いた頃は、午前中だけ落ちていた。
@pytest.mark.parametrize("post_time", ["00:05", "23:55"])
def test_見送りで空いた枠に後ろが詰まる(monkeypatch, tmp_path, post_time) -> None:
    """穴を残さない。順番は変えず、先頭の空き枠から詰め直す。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, [post_time])
    slots = _slots(config, 3)
    conn = _db(tmp_path)
    first = _post(conn, slots[0], "Day1")
    second = _post(conn, slots[1], "Day2")
    third = _post(conn, slots[2], "Day3")
    conn.close()

    assert main(["--config", config, "post", "skip", "--id", str(first)]) == 0
    assert main(["--config", config, "post", "compact"]) == 0

    conn = connect(tmp_path / "test.db")
    rows = {
        r["id"]: datetime.fromisoformat(r["scheduled_at"])
        for r in conn.execute("SELECT id, scheduled_at FROM posts").fetchall()
    }
    # 2番目が1つ前の枠へ、3番目がその次へ繰り上がる
    assert rows[second] == slots[0]
    assert rows[third] == slots[1]
    # 見送ったものは枠を空ける（**同じ日に2本並ばせない**）
    assert rows[first] > rows[third]


def test_公開済みは詰めても動かない(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["09:00"])
    conn = _db(tmp_path)
    done = _post(conn, _at(-1, "09:00"), "Done")
    finish_post(conn, done, "media-1", "c1")
    before = conn.execute("SELECT scheduled_at FROM posts WHERE id=?",
                          (done,)).fetchone()["scheduled_at"]
    _post(conn, _at(3, "09:00"), "Later")
    conn.close()

    assert main(["--config", config, "post", "compact"]) == 0
    conn = connect(tmp_path / "test.db")
    after = conn.execute("SELECT scheduled_at FROM posts WHERE id=?",
                         (done,)).fetchone()["scheduled_at"]
    assert after == before


def test_同じ日に2本並ばない(monkeypatch, tmp_path) -> None:
    """見送りが枠を塞いだまま詰めると、その日に2本出る（2026-08-22 の不具合）。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["09:00"])
    conn = _db(tmp_path)
    for day, title in ((1, "A"), (2, "B"), (3, "C"), (4, "D")):
        _post(conn, _at(day, "09:00"), title)
    ids = [r["id"] for r in conn.execute("SELECT id FROM posts ORDER BY id").fetchall()]
    conn.close()

    # 1日目と3日目を見送る
    for post_id in (ids[0], ids[2]):
        assert main(["--config", config, "post", "skip", "--id", str(post_id)]) == 0
    assert main(["--config", config, "post", "compact"]) == 0

    conn = connect(tmp_path / "test.db")
    days = [
        datetime.fromisoformat(r["scheduled_at"]).astimezone(JST).date()
        for r in conn.execute(
            "SELECT scheduled_at FROM posts WHERE state = 'planned'"
        ).fetchall()
    ]
    assert len(days) == len(set(days)), f"同じ日に複数の予定がある: {days}"


# --- 出た時刻を出す（2026-08-25） --------------------------------------
#
# 「定刻に出たか」は state が published というだけでは分からない。
# published_at は持っているのに一覧に出していなかったので、毎朝の確認で
# 「何分遅れたか」を答えられなかった。

def test_出た時刻と遅れを出す(tmp_path, capsys):
    config = _config(tmp_path, ["09:00"])
    conn = _db(tmp_path)
    slot = _at(0, "09:00")
    post_id = _post(conn, slot, "Late House")
    finish_post(conn, post_id, "media-1", "c1")
    conn.execute(
        "UPDATE posts SET published_at = ? WHERE id = ?",
        ((slot + timedelta(minutes=7)).isoformat(), post_id),
    )
    conn.commit()
    conn.close()

    main(["--config", config, "post", "show"])
    out = capsys.readouterr().out
    assert "09:07 +7分" in out


def test_定刻に出たものは定刻と出す(tmp_path, capsys):
    config = _config(tmp_path, ["09:00"])
    conn = _db(tmp_path)
    slot = _at(0, "09:00")
    post_id = _post(conn, slot, "On Time House")
    finish_post(conn, post_id, "media-2", "c2")
    conn.execute("UPDATE posts SET published_at = ? WHERE id = ?",
                 (slot.isoformat(), post_id))
    conn.commit()
    conn.close()

    main(["--config", config, "post", "show"])
    assert "09:00 定刻" in capsys.readouterr().out


def test_まだ出ていない予定には時刻を出さない(tmp_path, capsys):
    config = _config(tmp_path, ["09:00"])
    conn = _db(tmp_path)
    _post(conn, _at(1, "09:00"), "Future House")
    conn.close()

    main(["--config", config, "post", "show"])
    out = capsys.readouterr().out
    assert "Future House" in out
    assert "定刻" not in out and "分" not in out.split("Future House")[0].split("planned")[1]


# --- 時刻がずれた行があっても同じ日に2本入れない（2026-08-31） --------
#
# **実際に出てしまった。** post 5 が 08:59、post 21 が 09:00 で、同じ朝に
# 2本公開された。空き判定が時刻の完全一致だったため、08:59 の行があっても
# 枠 09:00 は「空いている」と見えていた。時刻は画面から手で直せるので、
# ずれた行はいつでも生まれる。

def _off_grid_post(conn, when: datetime, title: str) -> int:
    """枠からずれた時刻の予定を1件。"""
    return _post(conn, when, title)


def test_1分ずれた行があってもその日には入れない(monkeypatch, tmp_path) -> None:
    from datetime import timedelta

    from freming.config import load_config
    from freming.instagram.plan import holding_slots, open_slots, slot_times

    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["09:00"])
    cfg = load_config(config)
    now = datetime.now(UTC)
    slots = slot_times(cfg, now)
    conn = _db(tmp_path)
    # 先頭の枠の1分前に1本置く（画面で時刻を直したときに起きる形）
    _off_grid_post(conn, slots[0] - timedelta(minutes=1), "Off grid")
    occupied = holding_slots(conn, slots[-1] + timedelta(minutes=1))
    free = open_slots(cfg, slots, occupied)
    conn.close()

    assert slots[0] not in free, "1分ずれた行がある日を空きにしてはいけない"
    assert slots[1] in free


def test_見送りは枠を持たない(monkeypatch, tmp_path) -> None:
    """出さないと決めたものが枠を塞ぐと、その日の投稿が無くなる。"""
    from freming.config import load_config
    from freming.db.repository import skip_post
    from freming.instagram.plan import holding_slots, open_slots, slot_times

    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["09:00"])
    cfg = load_config(config)
    slots = slot_times(cfg, datetime.now(UTC))
    conn = _db(tmp_path)
    post_id = _post(conn, slots[0], "Skipped")
    skip_post(conn, post_id)
    occupied = holding_slots(conn, slots[-1] + timedelta(minutes=1))
    free = open_slots(cfg, slots, occupied)
    conn.close()

    assert slots[0] in free


def test_予定を作るときも1日1本を超えない(monkeypatch, tmp_path) -> None:
    """post plan が2本目を入れないこと。**08-31 に起きたのはこれ。**"""
    from datetime import timedelta

    from freming.config import load_config
    from freming.instagram.plan import slot_times

    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(tmp_path, ["09:00"])
    cfg = load_config(config)
    slots = slot_times(cfg, datetime.now(UTC))
    conn = _db(tmp_path)
    _off_grid_post(conn, slots[0] - timedelta(minutes=1), "Off grid")
    # 在庫を積んでおく（plan が埋めようとする材料）
    for n in range(3):
        cursor = conn.execute(
            "INSERT INTO properties (source, source_url, title, location_city, "
            "location_country, summary, score, status, collected_at, display_name, "
            "caption_body) VALUES ('dezeen', ?, ?, 'Porto', 'Portugal', 's', 70, "
            "'delivered', ?, ?, '本文') RETURNING id",
            (f"https://example.com/stock{n}", f"Stock {n}",
             datetime.now(UTC).isoformat(), f"Stock {n}"),
        )
        cursor.fetchone()
    conn.commit()
    conn.close()

    assert main(["--config", config, "post", "plan"]) == 0

    conn = connect(tmp_path / "test.db")
    first_day = slots[0].astimezone(JST).date()
    same_day = [
        r for r in conn.execute(
            "SELECT scheduled_at FROM posts WHERE kind = 'feed'"
        ).fetchall()
        if datetime.fromisoformat(r["scheduled_at"]).astimezone(JST).date() == first_day
    ]
    conn.close()
    assert len(same_day) == 1, f"同じ日に {len(same_day)} 本入った"
