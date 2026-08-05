"""審査UIのアクセス制限（Basic認証）。

審査UIは元々ローカル（127.0.0.1）専用で、認証を持たない作りだった。
担当者と一緒に審査するために外へ出せるようにしたが、**外向けに
待ち受けるときは資格情報を必須にする**。URLを知っているだけで承認・
非承認・納品の引き金を引けてしまうため。

資格情報は環境変数からのみ読む。config.yaml はリポジトリに入っており、
パスワードを置く場所ではない。

    REVIEW_UI_USER
    REVIEW_UI_PASSWORD

平文で流れる方式なので、**HTTPS でのみ使うこと**。Render のような
TLS 終端のあるホスティングを前提にしている。
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from freming.logging_setup import get_logger

log = get_logger(__name__)

REALM = "FREMING CURATED"

# 認証なしで待ち受けてよいアドレス。ここ以外に出すときは資格情報を要求する。
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@dataclass(frozen=True)
class BasicAuth:
    user: str
    password: str

    def matches(self, user: str, password: str) -> bool:
        """一致するか。

        両方を必ず比較する。ユーザ名が違った時点で返すと、応答時間から
        ユーザ名だけ当てられる余地が残る。
        """
        ok_user = secrets.compare_digest(self.user, user)
        ok_password = secrets.compare_digest(self.password, password)
        return ok_user and ok_password


def credentials_from_env() -> BasicAuth | None:
    """環境変数から資格情報を読む。片方だけの設定は設定漏れとして扱う。"""
    user = os.environ.get("REVIEW_UI_USER", "")
    password = os.environ.get("REVIEW_UI_PASSWORD", "")
    if not user and not password:
        return None
    if not user or not password:
        raise RuntimeError(
            "REVIEW_UI_USER と REVIEW_UI_PASSWORD は両方を設定してください"
            "（片方だけでは認証をかけません）"
        )
    return BasicAuth(user, password)


def is_public_host(host: str) -> bool:
    """ループバック以外＝外から届きうる、と見なす。"""
    return host not in _LOOPBACK


def require_credentials(host: str) -> BasicAuth | None:
    """待ち受けアドレスに見合う資格情報を返す。足りなければ止める。

    外向けのアドレスに認証なしで立ち上がる経路を作らないための歯止め。
    """
    auth = credentials_from_env()
    if auth is None and is_public_host(host):
        raise RuntimeError(
            f"{host} で待ち受けるには REVIEW_UI_USER と REVIEW_UI_PASSWORD が要ります。"
            "認証なしで公開すると、URLを知っている人が承認・非承認できます"
        )
    return auth


def _parse_header(value: str) -> tuple[str, str] | None:
    """Authorization ヘッダから user/password を取り出す。壊れていれば None。"""
    scheme, _, encoded = value.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    user, sep, password = decoded.partition(":")
    return (user, password) if sep else None


# 認証を通さない経路。どちらも物件のデータは一切返さない。
#
#   /healthz     ホスティング側のヘルスチェックが資格情報を送らないため
#   /ig/callback Instagram の認可後の着地先。@frmg.jpn の管理者は審査UIの
#                資格情報を持っていないので、ここで認証を求めると詰む。
#                返すのは「このURLをコピーして送ってください」の案内だけで、
#                受け取った code をサーバー側で使うことはしない（code から
#                トークンへの交換には app secret が要り、それは手元にしか
#                置かない）。
EXEMPT_PATHS = frozenset({"/healthz", "/ig/callback"})


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """EXEMPT_PATHS 以外の全経路に Basic 認証をかける。

    一覧も詳細も承認も、認証を通ってから触る。
    """

    def __init__(self, app, auth: BasicAuth) -> None:
        super().__init__(app)
        self._auth = auth

    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        parsed = _parse_header(header) if header else None
        if parsed is None or not self._auth.matches(*parsed):
            if header:
                # 失敗はログに残す。総当たりに気づけるようにするため。
                # 送られてきた値そのものは書かない。
                log.warning("認証に失敗しました: %s", request.client.host if request.client else "?")
            return Response(
                "認証が必要です",
                status_code=401,
                headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
            )
        return await call_next(request)


__all__ = [
    "BasicAuth",
    "BasicAuthMiddleware",
    "credentials_from_env",
    "is_public_host",
    "require_credentials",
]
