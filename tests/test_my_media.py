"""自分のアカウントの過去投稿を読む経路の検証。

手で運用していた頃の投稿を、週次リールの材料にするためのもの。
**読むのは自分の投稿だけ**で、他人のアカウントを読む経路は作らない。
"""

from __future__ import annotations

import pytest

from freming.instagram import mymedia
from freming.instagram.tokens import InstagramError

CAROUSEL = {
    "id": "1789",
    "media_type": "CAROUSEL_ALBUM",
    "permalink": "https://www.instagram.com/p/AAA/",
    "timestamp": "2026-08-19T03:00:00+0000",
    "caption": "・\n世界で今、売りに出ている家。\n\nDuckpond House",
    "children": {"data": [
        {"media_url": "https://cdn.example.com/1.jpg", "media_type": "IMAGE"},
        {"media_url": "https://cdn.example.com/2.jpg", "media_type": "IMAGE"},
    ]},
}
SINGLE = {
    "id": "1790",
    "media_type": "IMAGE",
    "permalink": "https://www.instagram.com/p/BBB/",
    "timestamp": "2026-08-18T03:00:00+0000",
    "caption": "・\nひとこと",
    "media_url": "https://cdn.example.com/single.jpg",
}


def _fake_request(body):
    def request(method, url, token, **kwargs):
        request.calls.append((method, url, kwargs.get("params", {})))
        return body
    request.calls = []
    return request


# --- 一覧 --------------------------------------------------------------
def test_カルーセルは1枚目を表紙にする(monkeypatch) -> None:
    monkeypatch.setattr(mymedia, "_request", _fake_request({"data": [CAROUSEL]}))
    item = mymedia.recent_media("t", "42")[0]
    assert item.image_url == "https://cdn.example.com/1.jpg"
    assert item.child_count == 2


def test_1枚の投稿はそのまま(monkeypatch) -> None:
    monkeypatch.setattr(mymedia, "_request", _fake_request({"data": [SINGLE]}))
    item = mymedia.recent_media("t", "42")[0]
    assert item.image_url == "https://cdn.example.com/single.jpg"
    assert item.child_count == 0


def test_見出しは本文の1行目の丸を飛ばす(monkeypatch) -> None:
    """全投稿の1行目は「・」。そのまま出すと一覧が「・」だけになる。"""
    monkeypatch.setattr(mymedia, "_request", _fake_request({"data": [CAROUSEL]}))
    assert mymedia.recent_media("t", "42")[0].head() == "世界で今、売りに出ている家。"


def test_本文が無くても落ちない(monkeypatch) -> None:
    body = {"data": [{**SINGLE, "caption": None}]}
    monkeypatch.setattr(mymedia, "_request", _fake_request(body))
    assert mymedia.recent_media("t", "42")[0].head() == "（本文なし）"


def test_自分のアカウントだけを読む(monkeypatch) -> None:
    """**他人のIDを渡す経路にしない。** 叩く先が /{自分のID}/media であること。"""
    request = _fake_request({"data": []})
    monkeypatch.setattr(mymedia, "_request", request)
    mymedia.recent_media("t", "17841400000000000", limit=4)
    method, url, params = request.calls[0]
    assert method == "GET"
    assert url.endswith("/17841400000000000/media")
    assert params["limit"] == 4


# --- 画像の保存 --------------------------------------------------------
class FakeResponse:
    def __init__(self, content: bytes, content_type: str) -> None:
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        pass


def test_画像を保存する(monkeypatch, tmp_path) -> None:
    import httpx

    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: FakeResponse(b"\xff\xd8\xff", "image/jpeg")
    )
    dest = tmp_path / "frames" / "01.jpg"
    mymedia.download_image("https://cdn.example.com/1.jpg", dest)
    assert dest.read_bytes() == b"\xff\xd8\xff"


def test_画像でなければ保存しない(monkeypatch, tmp_path) -> None:
    """CDNのURLは期限付き。切れるとHTMLのエラーページが返る。

    拡張子だけ見て書くと、リールを組む段で「壊れたJPEG」で落ちる。
    """
    import httpx

    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: FakeResponse(b"<html>expired</html>", "text/html")
    )
    dest = tmp_path / "01.jpg"
    with pytest.raises(InstagramError, match="期限付き"):
        mymedia.download_image("https://cdn.example.com/1.jpg", dest)
    assert not dest.exists()
