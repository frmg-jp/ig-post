"""[2] 投稿本文で使う項目の後追い埋めの検証。

**過去の審査結果を動かさないこと**が一番大事なので、そこを固定する。
納品済みこそ投稿に回るので、対象から外さないことも合わせて見る。
"""

from __future__ import annotations

import pytest

from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.scoring.backfill import FIELDS, backfill, estimate, pending_rows, save_fields
from freming.scoring.schema import Assessment

CONFIG = load_config("config.yaml")


class FakeClient:
    """記事を読んだつもりで、決まった値を返す。"""

    model = "fake"

    def __init__(self, **values) -> None:
        self.values = values
        self.calls = 0

    def assess(self, _prompt: str) -> Assessment:
        self.calls += 1
        return Assessment(**self.values)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    migrate(path)
    return connect(path)


def _add(conn, *, status="delivered", scored=True, text="本文", **columns) -> int:
    cursor = conn.execute(
        "INSERT INTO properties (source, source_url, title, content_text, summary, "
        "score, score_reason, status, collected_at, scored_at) "
        "VALUES ('dezeen', ?, 'A House', ?, '選定理由', 70, '根拠', ?, ?, ?) RETURNING id",
        (f"https://example.com/{id(columns)}{status}{scored}", text, status,
         "2026-08-01T00:00:00+00:00",
         "2026-08-01T00:00:00+00:00" if scored else None),
    )
    property_id = cursor.fetchone()["id"]
    if columns:
        sets = ", ".join(f"{k} = ?" for k in columns)
        conn.execute(f"UPDATE properties SET {sets} WHERE id = ?",
                     (*columns.values(), property_id))
    conn.commit()
    return property_id


# --- 対象の選び方 -----------------------------------------------------
def test_納品済みも対象になる(db):
    """納品済みこそ投稿に回る。rescore と逆にここは外さない。"""
    property_id = _add(db, status="delivered")
    assert [r["id"] for r in pending_rows(db)] == [property_id]


def test_納品済みが先に来る(db):
    later = _add(db, status="pending")
    first = _add(db, status="delivered")
    ids = [r["id"] for r in pending_rows(db)]
    assert ids[0] == first
    assert later in ids


def test_全部埋まっている行は対象にならない(db):
    _add(db, **{f: "x" for f in FIELDS})
    assert pending_rows(db) == []


def test_1つでも空いていれば対象になる(db):
    filled = {f: "x" for f in FIELDS}
    filled["photo_credit"] = None
    property_id = _add(db, **filled)
    assert [r["id"] for r in pending_rows(db)] == [property_id]


def test_未採点は対象にならない(db):
    """採点を通っていないものは、まず score から流す。"""
    _add(db, scored=False)
    assert pending_rows(db) == []


# --- 書き込み ---------------------------------------------------------
def test_足した列だけが埋まる(db):
    property_id = _add(db)
    client = FakeClient(
        usage_type="Private Residence", structure="Post-and-Beam",
        style_name="Mid-Century Modern", summary_en="A 1961 house.",
        photo_credit="Darren Bradley",
        # 採点まわりの値も返ってくるが、書かれてはいけない
        summary="別の要約", story_score=10, architect="別の建築家",
    )
    stats = backfill(CONFIG, db, client=client)
    assert stats.filled == 1

    row = db.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    assert row["usage_type"] == "Private Residence"
    assert row["style_name"] == "Mid-Century Modern"
    assert row["photo_credit"] == "Darren Bradley"


def test_スコアと審査結果は動かない(db):
    """**過去の審査を動かさない。** ここが崩れると履歴が壊れる。"""
    property_id = _add(db, status="delivered")
    before = db.execute(
        "SELECT score, score_reason, status, summary, scored_at FROM properties WHERE id = ?",
        (property_id,),
    ).fetchone()

    backfill(CONFIG, db, client=FakeClient(usage_type="Private Residence",
                                           summary="LLMの別の要約", story_score=0))

    after = db.execute(
        "SELECT score, score_reason, status, summary, scored_at FROM properties WHERE id = ?",
        (property_id,),
    ).fetchone()
    assert after["score"] == before["score"]
    assert after["score_reason"] == before["score_reason"]
    assert after["status"] == before["status"]
    assert after["summary"] == before["summary"]      # 人が見た文言を残す
    assert after["scored_at"] == before["scored_at"]


def test_既にある値は上書きしない(db):
    """人が直した値を、あとからLLMの出力で潰さない。"""
    property_id = _add(db, usage_type="人が直した用途")
    backfill(CONFIG, db, client=FakeClient(usage_type="LLMの用途", structure="Wood Frame"))
    row = db.execute("SELECT usage_type, structure FROM properties WHERE id = ?",
                     (property_id,)).fetchone()
    assert row["usage_type"] == "人が直した用途"
    assert row["structure"] == "Wood Frame"


def test_記事に記載が無ければ空のまま数える(db):
    _add(db)
    stats = backfill(CONFIG, db, client=FakeClient())
    assert stats.filled == 0
    assert stats.empty == 1


def test_本文が無ければAPIを呼ばない(db):
    """読ませようがないものに課金しない。"""
    _add(db, text="")
    client = FakeClient(usage_type="x")
    stats = backfill(CONFIG, db, client=client)
    assert stats.no_text == 1
    assert client.calls == 0


def test_空文字は書かない(db):
    property_id = _add(db, usage_type=None)
    assert save_fields(db, property_id, {"usage_type": "", "structure": "Wood"}) == 1
    row = db.execute("SELECT usage_type, structure FROM properties WHERE id = ?",
                     (property_id,)).fetchone()
    assert row["usage_type"] is None
    assert row["structure"] == "Wood"


# --- 費用の見積もり ---------------------------------------------------
def test_件数が増えれば見積もりも増える(db):
    _add(db)
    one = estimate(pending_rows(db))
    _add(db, status="pending")
    two = estimate(pending_rows(db))
    assert two[2] > one[2] > 0
