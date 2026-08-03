"""[1]〜[7] を通しで動かす結合テスト。

    収集 → 採点 → 審査 → 納品 → 学習 → 再採点

外部（HTTP / Claude / Drive）はすべて差し替える。個々のモジュールの
テストは各ファイルにあるので、ここで見るのは「工程の間で受け渡される
データが噛み合っているか」だけ。
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from freming.collect.editorial import EditorialCollector
from freming.config import load_config
from freming.db.connection import connect
from freming.db.migrate import migrate
from freming.db.repository import approved_rules, decide_rule_candidate
from freming.delivery.deliver import deliver_approved
from freming.learning.loop import run_learning
from freming.scoring.prompt import build_system_prompt
from freming.scoring.runner import score_pending
from freming.scoring.schema import Assessment
from freming.web.app import create_app

FEED_URL = "https://www.wowhaus.co.uk/feed/"
ARTICLE_URL = "https://www.wowhaus.co.uk/firehouse/"


def _png(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (140, 90, 70)).save(buffer, "PNG")
    return buffer.getvalue()


class _Response:
    def __init__(self, content, content_type: str = "text/html") -> None:
        if isinstance(content, str):
            self.text, self.content = content, content.encode("utf-8")
        else:
            self.content, self.text = content, ""
        self.headers = {"content-type": content_type}
        self.status_code = 200


def _feed() -> str:
    published = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<item><title>1894 firehouse conversion</title><link>{ARTICLE_URL}</link>"
        f"<pubDate>{published}</pubDate>"
        "<description><![CDATA[<p>The red arched engine doors still open onto the "
        "street. For sale at $1,250,000.</p>]]></description></item>"
        "</channel></rss>"
    )


class FakeHttp:
    def __init__(self) -> None:
        article = _Response(
                "<article><p>The 1894 firehouse retains its red arched engine doors "
                "and pressed tin ceiling. For sale at $1,250,000.</p>"
                '<img src="/photos/facade.jpg"><img src="/photos/interior.jpg">'
                "</article>"
        )
        self.pages = {
            FEED_URL: _Response(_feed(), "application/rss+xml"),
            # 収集時に normalize_url が末尾スラッシュを落とすため、両方を用意する
            ARTICLE_URL: article,
            ARTICLE_URL.rstrip("/"): article,
            "https://www.wowhaus.co.uk/photos/facade.jpg": _Response(
                _png(1600, 1200), "image/jpeg"
            ),
            "https://www.wowhaus.co.uk/photos/interior.jpg": _Response(
                _png(1400, 1400), "image/jpeg"
            ),
        }

    def get(self, url: str, **_kwargs):
        if url not in self.pages:
            raise RuntimeError(f"想定外のURL: {url}")
        return self.pages[url]

    def close(self) -> None:
        pass


class FakeDrive:
    def __init__(self) -> None:
        self.uploads: list[str] = []
        self.folders: list[str] = []

    def create_folder(self, name: str, parent_id: str) -> str:
        self.folders.append(name)
        return f"folder-{name}"

    def upload_file(self, local_path, name, parent_id, mime_type=""):
        self.uploads.append(name)

    def upload_bytes(self, data, name, parent_id, mime_type=""):
        self.uploads.append(name)


class FakeScoring:
    model = "fake-model"

    def __init__(self, assessment: Assessment) -> None:
        self.assessment = assessment

    def assess(self, user_prompt: str, **_kwargs) -> Assessment:
        return self.assessment


class FakeLearning:
    def classify(self, rows, tags):
        return {int(r["id"]): "no_visible_provenance" for r in rows}

    def propose_rule(self, tag, reasons, hits):
        return "転用前の痕跡が写真から読み取れない物件は story_score を下げる"


@pytest.fixture()
def config(tmp_path):
    cfg = load_config("config.yaml").model_copy(deep=True)
    cfg.app.db_path = tmp_path / "pipeline.db"
    cfg.images.work_dir = tmp_path / "images"
    migrate(cfg.app.db_path)
    return cfg


@pytest.fixture()
def conn(config):
    connection = connect(config.app.db_path)
    yield connection
    connection.close()


def test_collect_to_delivery(config, conn) -> None:
    """収集した候補が採点・承認を経て納品まで届くこと。"""
    source = config.editorial_source("wowhaus").model_copy(deep=True)
    source.feeds = [FEED_URL]
    http = FakeHttp()

    # [1] 収集
    collected = EditorialCollector(config, http, conn).collect(source)
    assert collected.inserted == 1

    # [2] 採点
    scored = score_pending(
        config,
        conn,
        client=FakeScoring(
            Assessment(
                is_for_sale=True, genre="adaptive_reuse", provenance_visible=True,
                provenance_note="消防車用のアーチ扉が現役", style_identified=True,
                one_of_a_kind=True, story_score=88,
                story_reason="1894年の消防署。アーチ扉とブリキ天井が残る",
                summary="1894年の消防署。赤いアーチ扉が玄関のまま",
                city="Chicago", country="United States", price="$1,250,000",
            )
        ),
    )
    assert scored.scored == 1

    row = conn.execute("SELECT * FROM properties").fetchone()
    assert row["score"] > config.scoring.thresholds.min_to_persist
    assert row["provenance_visible"] == 1

    # [3] 審査（承認）
    client = TestClient(create_app(config))
    client.post(f"/p/{row['id']}/approve")

    # [4][5][6] 画像取得 → 加工 → 納品
    drive = FakeDrive()
    delivered = deliver_approved(config, conn, drive=drive, http=FakeHttp())

    assert len(delivered.delivered) == 1
    assert drive.folders == ["frmg_ig001"]
    assert drive.uploads == ["01.jpg", "02.jpg", "meta.txt"]
    assert conn.execute(
        "SELECT status FROM properties WHERE id = ?", (row["id"],)
    ).fetchone()["status"] == "delivered"

    # 再実行しても重複納品しない
    again = FakeDrive()
    deliver_approved(config, conn, drive=again, http=FakeHttp())
    assert again.uploads == []


def test_rejection_feeds_back_into_scoring(config, conn) -> None:
    """不承認 → 学習 → 承認したルールが次の採点プロンプトに載ること。"""
    source = config.editorial_source("wowhaus").model_copy(deep=True)
    source.feeds = [FEED_URL]
    EditorialCollector(config, FakeHttp(), conn).collect(source)
    row = conn.execute("SELECT * FROM properties").fetchone()

    # [3] 理由をつけて不承認
    client = TestClient(create_app(config))
    threshold = config.scoring.feedback.rule_candidate_min_hits
    for i in range(threshold):
        client.post(f"/p/{row['id']}/reject", data={"reason": f"痕跡が残っていない {i}"})
        client.post(f"/p/{row['id']}/reset")

    assert conn.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()["n"] == threshold

    # [7] 学習：分類してルール候補を作る
    stats = run_learning(config, conn, client=FakeLearning())
    assert len(stats.new_candidates) == 1

    # 承認するまではプロンプトに載らない
    assert approved_rules(conn) == []

    decide_rule_candidate(conn, "no_visible_provenance", "approved")
    prompt = build_system_prompt(config, [], approved_rules(conn))
    assert "痕跡が写真から読み取れない物件は story_score を下げる" in prompt
