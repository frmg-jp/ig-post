"""[9] 投稿ワーカーは**次の予定まで寝る**。

2026-09-17、サイト全体が8日間止まった。原因はこのワーカーが60秒ごとに
DBへ接続し続けていたこと。Render は Starter で24時間動いているので、
**Neon の計算時間が一度も休まず**、無料枠を使い切って接続が拒否された。
収集・採点・納品・投稿・レポートが全部道連れになった。

投稿を定刻に出すために常駐させた判断（2026-08-24）が、そのまま
DBを起こし続けていた。片方だけ見ていて繋がっていなかった。

ここで確かめるのは3つ:

  - 次の予定まで寝る（起きる回数がDBの請求に直結する）
  - **定刻には遅れない**（寝る長さは「次の予定まで」）
  - 予定を変えたら起こせる（寝たまま取り残さない）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import next_due_at
from freming.instagram.worker import PostingWorker

NOW = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "sleep.db"
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


def _plan(conn, post_id: int, at: datetime, *, kind="feed", state="planned", attempts=0):
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, attempts) VALUES (?, ?, ?, ?, ?)",
        (post_id, kind, state, at.isoformat(), attempts),
    )
    conn.commit()


# --- 次の予定を読む -----------------------------------------------------

def test_次の予定の時刻を返す(config, conn) -> None:
    _plan(conn, 1, NOW + timedelta(hours=8))
    _plan(conn, 2, NOW + timedelta(hours=3))      # こちらが先
    assert next_due_at(conn, 3) == (NOW + timedelta(hours=3)).isoformat()


def test_予定が無ければNone(config, conn) -> None:
    assert next_due_at(conn, 3) is None


def test_出し終わったものは見ない(config, conn) -> None:
    _plan(conn, 1, NOW + timedelta(hours=1), state="published")
    assert next_due_at(conn, 3) is None


def test_打ち切ったものは見ない(config, conn) -> None:
    """**claim_due_post と同じ条件で見る。** 片方だけ直すとズレる。"""
    _plan(conn, 1, NOW + timedelta(hours=1), attempts=3)
    assert next_due_at(conn, 3) is None
    assert next_due_at(conn, 4) is not None       # 上限を上げれば対象に戻る


def test_担当しない種別は見ない(config, conn) -> None:
    """リールは ffmpeg が要るので審査UIでは出さない。起きる理由にならない。"""
    _plan(conn, 1, NOW + timedelta(hours=1), kind="reel")
    assert next_due_at(conn, 3, ("feed", "story")) is None


# --- 寝る長さ -----------------------------------------------------------

def test_次の予定まで寝る(config, conn) -> None:
    """**ここがDBの請求に直結する。** 60秒で回すと Neon が休まない。"""
    _plan(conn, 1, NOW + timedelta(minutes=20))
    worker = PostingWorker(config)
    assert worker._sleep_for(conn, NOW) == pytest.approx(20 * 60, abs=1)


def test_定刻には遅れない(config, conn) -> None:
    """寝る長さは「次の予定まで」。予定を飛び越して寝ない。"""
    _plan(conn, 1, NOW + timedelta(minutes=3))
    worker = PostingWorker(config)
    delay = worker._sleep_for(conn, NOW)
    assert delay <= 3 * 60 + 1
    assert NOW + timedelta(seconds=delay) <= NOW + timedelta(minutes=3)


def test_予定が無くてもたまには起きる(config, conn) -> None:
    """外から予定を入れられることがある。寝たきりにはしない。"""
    worker = PostingWorker(config)
    assert worker._sleep_for(conn, NOW) == config.instagram.max_sleep_sec


def test_遠い予定でも上限で止める(config, conn) -> None:
    _plan(conn, 1, NOW + timedelta(days=3))
    worker = PostingWorker(config)
    assert worker._sleep_for(conn, NOW) == config.instagram.max_sleep_sec


def test_出せないまま短く回り続けない(config, conn) -> None:
    """**ここが 2026-09-17 の再発点。**

    時間が来ているのに1本も出せないのは、たいてい設定の問題（トークン
    切れ・public_base_url 未設定）で、待っても直らない。60秒で回り
    続けると、直らないまま24時間DBを起こし続ける。
    """
    _plan(conn, 1, NOW - timedelta(hours=5))
    worker = PostingWorker(config)
    assert worker._sleep_for(conn, NOW, progressed=False) == config.instagram.max_sleep_sec


def test_出せた直後は続けて見る(config, conn) -> None:
    """同じ時刻にもう1本溜まっていることがある。それは続けて出す。"""
    _plan(conn, 1, NOW - timedelta(minutes=1))
    worker = PostingWorker(config)
    assert worker._sleep_for(conn, NOW, progressed=True) == config.instagram.poll_interval_sec


def test_1日1本なら起きるのは数回(config, conn) -> None:
    """**60秒ポーリングは1日1440回。** そこが枠を食い切った理由。

    出せたら planned から消える（実際の動き）ぶんも含めて数える。
    """
    _plan(conn, 1, NOW + timedelta(hours=8))
    worker = PostingWorker(config)
    wakes, clock = 0, NOW
    limit = NOW + timedelta(days=1)
    while clock < limit and wakes < 200:
        delay = worker._sleep_for(conn, clock)
        clock += timedelta(seconds=delay)
        wakes += 1
        # 時間が来たら出して、予定から消す（実機と同じ）
        conn.execute(
            "UPDATE posts SET state = 'published' "
            "WHERE state = 'planned' AND scheduled_at <= ?",
            (clock.isoformat(),),
        )
        conn.commit()
    assert wakes <= 50, f"1日{wakes}回起きるなら、また枠を食う"


def test_起きる回数が請求に直結することを忘れない(config, conn) -> None:
    """上限を短くすると起きる回数が増える。**変えるときは請求を見る。**"""
    assert config.instagram.max_sleep_sec >= 900, (
        "15分より短くすると、Neon の compute がほぼ休まなくなる"
    )


def test_DBが読めなければ長めに寝る(config, conn) -> None:
    """**読めない理由が枠切れのこともある。** そこで短く回すと傷を広げる。"""
    class _Broken:
        def execute(self, *_a, **_k):
            raise RuntimeError("compute time quota")

    worker = PostingWorker(config)
    assert worker._sleep_for(_Broken(), NOW) == config.instagram.max_sleep_sec


# --- 起こす -------------------------------------------------------------

def test_起こせる(config) -> None:
    worker = PostingWorker(config)
    assert not worker._wakeup.is_set()
    worker.wake()
    assert worker._wakeup.is_set()


def _counting_worker(monkeypatch) -> dict:
    """ワーカーを起こさずに、起こされた回数だけ数える。"""
    calls = {"wake": 0}
    monkeypatch.setattr(PostingWorker, "start", lambda self: None)
    monkeypatch.setattr(PostingWorker, "stop", lambda self: None)
    monkeypatch.setattr(
        PostingWorker, "wake", lambda self: calls.__setitem__("wake", calls["wake"] + 1)
    )
    return calls


def test_予定を触ると起きる(config, conn, monkeypatch) -> None:
    """**1か所で受ける。** 経路ごとに呼ぶと、足したときに付け忘れる。"""
    from fastapi.testclient import TestClient

    from freming.web.app import create_app

    _plan(conn, 1, NOW + timedelta(hours=5))
    config.instagram.auto_post = True
    calls = _counting_worker(monkeypatch)

    with TestClient(create_app(config)) as client:
        client.post("/posts/1/skip")
    assert calls["wake"] >= 1, "寝たまま取り残される"


def test_読むだけでは起こさない(config, conn, monkeypatch) -> None:
    """画面を開くたびに起こすと、結局DBを起こし続ける。"""
    from fastapi.testclient import TestClient

    from freming.web.app import create_app

    config.instagram.auto_post = True
    calls = _counting_worker(monkeypatch)

    with TestClient(create_app(config)) as client:
        client.get("/schedule")
        client.get("/healthz")
    assert calls["wake"] == 0


# --- 溜まった予定を一斉に出さない ---------------------------------------

def test_古い枠は出さない(config, conn) -> None:
    """**2026-09-17 の停止で9本が溜まった。**

    DBが戻った瞬間に全部が数分で出るところだった。`post reschedule` の
    コメントには危険と書いてあったのに、止める仕掛けが無かった。
    """
    from freming.db.repository import claim_due_post

    old = (NOW - timedelta(days=3)).isoformat()
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, attempts) "
        "VALUES (1, 'feed', 'planned', ?, 0)", (old,),
    )
    conn.commit()

    cutoff = (NOW - timedelta(hours=config.instagram.stale_after_hours)).isoformat()
    assert claim_due_post(conn, NOW.isoformat(), 3, ("feed",), cutoff) is None
    # **消さない。** 消すと二度と投稿候補に戻らない
    row = conn.execute("SELECT state FROM posts WHERE id = 1").fetchone()
    assert row["state"] == "planned"


def test_少し遅れたものは出す(config, conn) -> None:
    """通常の遅れ（最長30分）で止めてしまっては、ただ出なくなる。"""
    from freming.db.repository import claim_due_post

    late = (NOW - timedelta(minutes=40)).isoformat()
    conn.execute(
        "INSERT INTO posts (id, kind, state, scheduled_at, attempts) "
        "VALUES (1, 'feed', 'planned', ?, 0)", (late,),
    )
    conn.commit()

    cutoff = (NOW - timedelta(hours=config.instagram.stale_after_hours)).isoformat()
    assert claim_due_post(conn, NOW.isoformat(), 3, ("feed",), cutoff) is not None


def test_古い枠は起きる理由にもしない(config, conn) -> None:
    """揃えないと、溜まった古い予定を見て短く回り続ける。"""
    _plan(conn, 1, NOW - timedelta(days=3))
    worker = PostingWorker(config)
    assert worker._sleep_for(conn, NOW) == config.instagram.max_sleep_sec


def test_run_onceは溜まった分を一斉に出さない(config, conn, monkeypatch) -> None:
    """**ここが本番の被害になるところ。** 9本が数分で出る。"""
    from freming.instagram import worker as mod

    for i in range(9):
        _plan(conn, i + 1, NOW - timedelta(days=9 - i))
    monkeypatch.setattr(mod, "load_token", lambda _c: type("R", (), {"value": "t"})())
    monkeypatch.setattr(mod, "account_id", lambda _t: "ig1")
    published = []
    monkeypatch.setattr(
        mod, "publish_one",
        lambda *a, **k: published.append(a[2]["id"]),
    )
    config.instagram.public_base_url = "https://example.com"

    result = mod.run_once(config, conn, now=NOW)
    assert published == [], f"{len(published)}本が一斉に出た"
    assert result.done == 0


# --- 人が押した経路では古い枠も出す（2026-10-01） ---------------------
# 古い枠を止める歯止めが、人が押すリールの公開まで止めていた。試写は
# 定期実行で 19:00 から3〜10時間遅れて組まれ、人が見て押す頃には必ず
# 6時間を過ぎている。**毎週弾かれる作りだった**（2026-09-29、post 48）。

def _fake_publish(monkeypatch) -> list:
    from freming.instagram import worker as mod

    monkeypatch.setattr(mod, "load_token", lambda _c: type("R", (), {"value": "t"})())
    monkeypatch.setattr(mod, "account_id", lambda _t: "ig1")
    published: list = []
    monkeypatch.setattr(mod, "publish_one", lambda *a, **k: published.append(a[2]["id"]))
    return published


def test_人が押せば古いリールも出る(config, conn, monkeypatch) -> None:
    """月曜 19:00 の枠を、火曜の朝に人が押して出す。いつもの流れ。"""
    from freming.instagram import worker as mod

    published = _fake_publish(monkeypatch)
    config.instagram.public_base_url = "https://example.com"
    _plan(conn, 48, NOW - timedelta(hours=14), kind="reel")

    result = mod.run_once(config, conn, now=NOW, limit=1, kinds=("reel",),
                          include_stale=True)
    assert published == [48]
    assert result.done == 1


def test_人が押しても1本だけ(config, conn, monkeypatch) -> None:
    """**歯止めの役目は件数の上限が引き継ぐ。** 9本溜まっていても1本。"""
    from freming.instagram import worker as mod

    published = _fake_publish(monkeypatch)
    config.instagram.public_base_url = "https://example.com"
    for i in range(9):
        _plan(conn, i + 1, NOW - timedelta(days=9 - i))

    mod.run_once(config, conn, now=NOW, limit=1, include_stale=True)
    assert published == [1], "古い順に1本だけ"


def test_上限なしでは古い枠を出せない(config, conn) -> None:
    """上限なしで歯止めを外すと、2026-09-17 の9本がまとめて出る形に戻る。"""
    from freming.instagram import worker as mod

    with pytest.raises(ValueError):
        mod.run_once(config, conn, now=NOW, include_stale=True)


def test_常駐ワーカーは古い枠を出さないまま(config, conn, monkeypatch) -> None:
    """**無人の経路は変えていない。** PostingWorker は include_stale を渡さない。"""
    import inspect

    from freming.instagram import worker as mod

    source = inspect.getsource(PostingWorker)
    assert "include_stale" not in source
    published = _fake_publish(monkeypatch)
    config.instagram.public_base_url = "https://example.com"
    _plan(conn, 48, NOW - timedelta(hours=14), kind="reel")
    mod.run_once(config, conn, now=NOW, limit=1, kinds=("reel",))
    assert published == []


def test_CLIは上限なしの指定を断る(config, monkeypatch, capsys) -> None:
    from freming import cli

    monkeypatch.setattr(cli, "load_config", lambda *_a, **_k: config)
    config.instagram.public_base_url = "https://example.com"
    code = cli.main(["post", "run", "--include-stale"])
    assert code == 2
    assert "--limit" in capsys.readouterr().err
