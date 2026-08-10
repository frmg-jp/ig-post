"""[9] 投稿のリーチを読む。週次リールに載せる7枚を選ぶために使う。

**このモジュールは追加のスコープを要求する。** 今のトークンは
`instagram_business_basic` と `instagram_business_content_publish` しか
持っていない。リーチには `instagram_business_manage_insights` が要る。
スコープはリフレッシュでは増やせないので、**認可をやり直すまで動かない。**

そこを黙って迂回しない。取れなければ MissingInsightsScope を上げ、
呼び出し側（週次リール）は「なぜ選べなかったか」を予定表に残す。
勝手に別の7枚で作ると、狙いと違うものが出たことに誰も気づけない。

各日の3投稿の中で1位を選ぶ形にしているのは、リーチが時間とともに
伸びるため。7日前と昨日を直接比べると昨日が必ず負ける。同じ日の
3本同士なら成熟度が揃う。
"""

from __future__ import annotations

from freming.instagram.publish import API_VERSION, GRAPH, _request
from freming.instagram.tokens import InstagramError
from freming.logging_setup import get_logger

log = get_logger(__name__)

SCOPE = "instagram_business_manage_insights"


class MissingInsightsScope(InstagramError):
    """トークンにインサイトの権限が無い。再認可が要る。"""


def _looks_like_scope_error(message: str) -> bool:
    lowered = message.lower()
    return "permission" in lowered or "scope" in lowered or "(#10)" in lowered


def media_reach(token: str, media_id: str) -> int | None:
    """1投稿のリーチ。取れなければ None（まだ集計されていない場合など）。"""
    try:
        body = _request(
            "GET", f"{GRAPH}/{API_VERSION}/{media_id}/insights", token,
            params={"metric": "reach"},
        )
    except InstagramError as exc:
        if _looks_like_scope_error(str(exc)):
            raise MissingInsightsScope(
                "トークンにインサイトの権限がありません。\n"
                f"リーチを読むには {SCOPE} が要りますが、いまのトークンには "
                "入っていません。スコープはリフレッシュでは増やせないので、\n"
                "  python -m freming.cli instagram auth-url\n"
                "のURLで認可をやり直してください。"
            ) from exc
        raise

    for row in body.get("data") or []:
        if row.get("name") != "reach":
            continue
        values = row.get("values") or []
        if values and values[0].get("value") is not None:
            return int(values[0]["value"])
    return None


def has_insights_scope(token: str) -> bool:
    """このトークンでリーチが読めるか。

    **投稿が1本も無くても確かめられるようにする。** 投稿ごとの
    insights は media_id が要るので、再認可の直後には試せない。
    アカウント単位の insights は同じスコープを要求するので、
    権限の有無だけならこちらで分かる。

    権限以外の理由（一時的な障害など）で落ちた場合は、その例外を
    そのまま上げる。**「取れなかった」を「権限が無い」に丸めない。**
    """
    try:
        _request(
            "GET", f"{GRAPH}/{API_VERSION}/me/insights", token,
            params={"metric": "reach", "period": "day"},
        )
    except InstagramError as exc:
        if _looks_like_scope_error(str(exc)):
            return False
        raise
    return True


__all__ = ["SCOPE", "MissingInsightsScope", "media_reach", "has_insights_scope"]
