"""[9] Instagram への投稿。

    コンテナを作る → 出来上がるのを待つ → 公開する

Meta の Content Publishing API は2段構え。いきなり投稿はできず、まず
「コンテナ」を作り、それが FINISHED になってから publish する。画像は
たいてい即座に終わるが、動画（リール）は変換に時間がかかる。

守っていること:

  - **画像は公開URLで渡す。** Meta がこちらへ取りに来る。ローカルの
    パスやDriveのリンクは渡せない（instagram/media.py）。
  - **動画はファイルを直接送る。** リールには resumable upload があるので、
    公開URLを用意しなくてよい。数MBの動画をDBに置かずに済む。
  - コンテナの状態確認は1分おき・最大5分（Meta の推奨どおり）。
  - 24時間で100件という投稿上限がある。1日3投稿＋ストーリーズ＋週1リールなら
    まったく届かないが、暴走したときに気づけるよう、上限に近づいたら
    ログに出す。

ストーリーズは**ビジネスアカウントのみ**。クリエイターアカウントだと
コンテナ作成で弾かれる。そのエラーは握り潰さず、そのまま上げる。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from freming.instagram.tokens import InstagramError
from freming.logging_setup import get_logger

log = get_logger(__name__)

GRAPH = "https://graph.instagram.com"
API_VERSION = "v23.0"
UPLOAD_HOST = "https://rupload.facebook.com/ig-api-upload"

# コンテナの出来上がりを待つ間隔と回数。Meta の推奨は「1分おきに5分まで」。
POLL_INTERVAL_SEC = 20.0
POLL_MAX_SEC = 300.0

KIND_FEED = "feed"
KIND_STORY = "story"
KIND_REEL = "reel"


@dataclass
class PublishResult:
    media_id: str
    container_id: str


def _request(method: str, url: str, token: str, **kwargs) -> dict:
    """Graph API を叩く。Meta のエラー本文を読める形にして上げ直す。

    収集用の HttpClient は通さない。あれは相手サイトのクロール用で、
    robots.txt の確認やドメイン間隔が付いてくる。API には要らない
    （tokens.py と同じ判断）。
    """
    import httpx

    params = kwargs.pop("params", {})
    params["access_token"] = token
    response = httpx.request(method, url, params=params, timeout=120, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        error = body.get("error", {})
        message = error.get("message") or response.text[:300]
        raise InstagramError(
            f"Graph API が {response.status_code} を返しました: {message}"
        )
    return body


def account_id(token: str) -> str:
    """投稿先のアカウントID。"""
    body = _request("GET", f"{GRAPH}/me", token, params={"fields": "user_id,username"})
    found = body.get("user_id") or body.get("id")
    if not found:
        raise InstagramError(f"アカウントIDを取得できませんでした: {body}")
    return str(found)


# ----------------------------------------------------------------------
# コンテナ
# ----------------------------------------------------------------------
def create_image_container(
    token: str, ig_id: str, image_url: str, caption: str | None = None,
    *, story: bool = False, alt_text: str | None = None, carousel_item: bool = False,
) -> str:
    """画像のコンテナを作る。story=True でストーリーズ。

    carousel_item=True はカルーセルの1枚。キャプションは親のカルーセル
    コンテナに付けるので、ここでは送らない。

    ストーリーズはキャプションも alt_text も受け付けないので送らない
    （送っても無視される）。
    """
    params: dict[str, str] = {"image_url": image_url}
    if story:
        params["media_type"] = "STORIES"
    elif carousel_item:
        params["is_carousel_item"] = "true"
        if alt_text:
            params["alt_text"] = alt_text
    else:
        if caption:
            params["caption"] = caption
        # 代替テキスト。読み上げと検索に効く。2025-03 に通常投稿へ追加された
        # （リールとストーリーズは対象外）。
        if alt_text:
            params["alt_text"] = alt_text
    body = _request("POST", f"{GRAPH}/{API_VERSION}/{ig_id}/media", token, params=params)
    container = body.get("id")
    if not container:
        raise InstagramError(f"コンテナIDが返りませんでした: {body}")
    return str(container)


def create_carousel_container(
    token: str, ig_id: str, children: list[str], caption: str | None = None
) -> str:
    """カルーセル（複数枚投稿）の親コンテナを作る。

    children は先に作った各枚のコンテナID。並びがそのまま表示順になる。
    キャプションは親にだけ付ける。
    """
    params: dict[str, str] = {
        "media_type": "CAROUSEL",
        "children": ",".join(children),
    }
    if caption:
        params["caption"] = caption
    body = _request("POST", f"{GRAPH}/{API_VERSION}/{ig_id}/media", token, params=params)
    container = body.get("id")
    if not container:
        raise InstagramError(f"カルーセルのコンテナIDが返りませんでした: {body}")
    return str(container)


def create_reel_container(token: str, ig_id: str, caption: str | None = None) -> str:
    """リールのコンテナを作る。中身はこのあと upload_video で送る。"""
    params = {"media_type": "REELS", "upload_type": "resumable"}
    if caption:
        params["caption"] = caption
    body = _request("POST", f"{GRAPH}/{API_VERSION}/{ig_id}/media", token, params=params)
    container = body.get("id")
    if not container:
        raise InstagramError(f"コンテナIDが返りませんでした: {body}")
    return str(container)


def upload_video(token: str, container_id: str, video: Path) -> None:
    """動画の実体を送る。公開URLが要らないのはこの経路があるから。"""
    import httpx

    data = video.read_bytes()
    response = httpx.post(
        f"{UPLOAD_HOST}/{API_VERSION}/{container_id}",
        headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(len(data)),
            "Content-Type": "application/octet-stream",
        },
        content=data,
        timeout=600,
    )
    if response.status_code != 200:
        raise InstagramError(
            f"動画の送信が {response.status_code} で失敗しました: {response.text[:300]}"
        )


def container_status(token: str, container_id: str) -> str:
    body = _request(
        "GET", f"{GRAPH}/{API_VERSION}/{container_id}", token,
        params={"fields": "status_code,status"},
    )
    return str(body.get("status_code") or "")


def wait_until_ready(
    token: str, container_id: str, *, sleep=time.sleep, now=time.monotonic
) -> None:
    """FINISHED になるまで待つ。ERROR / EXPIRED はそのまま上げる。"""
    started = now()
    while True:
        state = container_status(token, container_id)
        if state == "FINISHED":
            return
        if state in ("ERROR", "EXPIRED"):
            raise InstagramError(
                f"コンテナが {state} になりました（container_id={container_id}）。"
                "画像URLに到達できなかったか、形式が受け付けられていません。"
            )
        if now() - started > POLL_MAX_SEC:
            raise InstagramError(
                f"コンテナが {POLL_MAX_SEC:.0f} 秒たっても仕上がりません"
                f"（container_id={container_id} / 最後の状態 {state or '不明'}）。"
            )
        sleep(POLL_INTERVAL_SEC)


def publish_container(token: str, ig_id: str, container_id: str) -> str:
    body = _request(
        "POST", f"{GRAPH}/{API_VERSION}/{ig_id}/media_publish", token,
        params={"creation_id": container_id},
    )
    media_id = body.get("id")
    if not media_id:
        raise InstagramError(f"投稿IDが返りませんでした: {body}")
    return str(media_id)


def media_permalink(token: str, media_id: str) -> str | None:
    """投稿のURL。ストーリーズへ手で追加するときに開く先。

    `instagram_business_basic` で取れる（インサイトの権限は要らない）。
    取れなくても投稿自体は成立しているので、失敗は None にして流す。
    """
    try:
        body = _request(
            "GET", f"{GRAPH}/{API_VERSION}/{media_id}", token,
            params={"fields": "permalink"},
        )
    except InstagramError as exc:
        log.warning("permalink を取れませんでした（media_id=%s）: %s", media_id, exc)
        return None
    return body.get("permalink")


def publishing_limit(token: str, ig_id: str) -> tuple[int, int]:
    """直近24時間の投稿数と上限。暴走に気づくために見る。"""
    body = _request(
        "GET", f"{GRAPH}/{API_VERSION}/{ig_id}/content_publishing_limit", token,
        params={"fields": "config,quota_usage"},
    )
    rows = body.get("data") or [{}]
    row = rows[0]
    used = int(row.get("quota_usage") or 0)
    limit = int((row.get("config") or {}).get("quota_total") or 100)
    return used, limit


# ----------------------------------------------------------------------
# まとめ
# ----------------------------------------------------------------------
def publish_image(
    token: str, ig_id: str, image_url: str, caption: str | None = None,
    *, story: bool = False, alt_text: str | None = None, sleep=time.sleep,
) -> PublishResult:
    container = create_image_container(
        token, ig_id, image_url, caption, story=story, alt_text=alt_text
    )
    wait_until_ready(token, container, sleep=sleep)
    media_id = publish_container(token, ig_id, container)
    log.info("投稿しました（%s）: media_id=%s", "ストーリーズ" if story else "通常", media_id)
    return PublishResult(media_id=media_id, container_id=container)


def publish_carousel(
    token: str, ig_id: str, image_urls: list[str], caption: str | None = None,
    *, alt_text: str | None = None, sleep=time.sleep,
) -> PublishResult:
    """複数枚を1つの投稿として出す。

    各枚のコンテナを作って仕上がりを待ち、親のカルーセルにまとめて公開する。
    1枚しか無いときは呼ばない（publish_image を使う）。
    """
    if len(image_urls) < 2:
        raise InstagramError("カルーセルには2枚以上要ります。1枚なら publish_image を使ってください。")
    children = []
    for url in image_urls:
        child = create_image_container(
            token, ig_id, url, carousel_item=True, alt_text=alt_text
        )
        wait_until_ready(token, child, sleep=sleep)
        children.append(child)
    container = create_carousel_container(token, ig_id, children, caption)
    wait_until_ready(token, container, sleep=sleep)
    media_id = publish_container(token, ig_id, container)
    log.info("投稿しました（カルーセル %d枚）: media_id=%s", len(children), media_id)
    return PublishResult(media_id=media_id, container_id=container)


def publish_reel(
    token: str, ig_id: str, video: Path, caption: str | None = None, *, sleep=time.sleep
) -> PublishResult:
    container = create_reel_container(token, ig_id, caption)
    upload_video(token, container, video)
    wait_until_ready(token, container, sleep=sleep)
    media_id = publish_container(token, ig_id, container)
    log.info("リールを投稿しました: media_id=%s", media_id)
    return PublishResult(media_id=media_id, container_id=container)


__all__ = [
    "KIND_FEED",
    "KIND_REEL",
    "KIND_STORY",
    "PublishResult",
    "account_id",
    "container_status",
    "create_carousel_container",
    "create_image_container",
    "create_reel_container",
    "media_permalink",
    "publish_container",
    "publish_carousel",
    "publish_image",
    "publish_reel",
    "publishing_limit",
    "upload_video",
    "wait_until_ready",
]
