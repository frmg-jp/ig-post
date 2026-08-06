"""Instagram Graph API の長期トークン管理。

    set-token（人が1回だけ貼る） → DBに保管 → 定期実行が毎日リフレッシュ

60日ルール（ここを外すと再認可のやり直しになる）:

  - 長期トークンの寿命は60日
  - 取得から24時間経つと更新でき、更新するとまた60日に戻る
  - **60日間一度も更新しないと失効し、復旧できない。** 担当者に
    もう一度認可してもらうしかない

トークンを .env や GitHub Secrets に置かないのは、リフレッシュのたびに
**新しいトークン**が発行されるため。静的な置き場では更新した値を書き
戻せない。審査UI・定期実行・納品が既に共有しているDB（api_tokens）に
置けば、更新は1箇所で済む。DBを読める者はトークンも読める点は
承知の上で使う（DATABASE_URL を持つ場所は既に納品まで担える）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from freming.db.connection import DbConnection
from freming.logging_setup import get_logger

log = get_logger(__name__)

GRAPH_BASE = "https://graph.instagram.com"
TOKEN_NAME = "instagram"

# 新規保存時に仮置きする寿命。ダッシュボードで生成した長期トークンは60日。
# 実際の残り時間は最初のリフレッシュで expires_in として返り、上書きされる。
INITIAL_LIFETIME = timedelta(days=60)
# 取得から24時間はリフレッシュできない（Meta側の制約）。
MIN_AGE_FOR_REFRESH = timedelta(hours=24)


class InstagramError(RuntimeError):
    """Graph API の失敗。Meta のエラーメッセージを含めて上げる。"""


@dataclass
class TokenRecord:
    value: str
    refreshed_at: datetime
    expires_at: datetime

    def age(self, now: datetime) -> timedelta:
        return now - self.refreshed_at

    def days_left(self, now: datetime) -> float:
        return (self.expires_at - now).total_seconds() / 86400


def _http_get(url: str, params: dict) -> dict:
    """Graph API への GET。Meta のエラー本文を読める形で上げ直す。

    収集用の HttpClient は通さない。あれは相手サイトへのクロール用で、
    robots.txt の確認やドメイン間隔の制御が付いてくる。API呼び出しには
    どちらも不要（Drive や Claude API と同じ扱い）。
    """
    import httpx

    response = httpx.get(url, params=params, timeout=30)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        error = body.get("error", {})
        message = error.get("message") or response.text[:200]
        raise InstagramError(f"Graph API が {response.status_code} を返しました: {message}")
    return body


def load_token(conn: DbConnection) -> TokenRecord | None:
    row = conn.execute(
        "SELECT value, refreshed_at, expires_at FROM api_tokens WHERE name = ?",
        (TOKEN_NAME,),
    ).fetchone()
    if row is None:
        return None
    return TokenRecord(
        value=row["value"],
        refreshed_at=datetime.fromisoformat(row["refreshed_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
    )


def save_token(
    conn: DbConnection,
    value: str,
    lifetime_sec: int | None = None,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    lifetime = timedelta(seconds=lifetime_sec) if lifetime_sec else INITIAL_LIFETIME
    expires = now + lifetime
    # SQLite/Postgres 両対応の UPSERT。name は PRIMARY KEY。
    conn.execute("DELETE FROM api_tokens WHERE name = ?", (TOKEN_NAME,))
    conn.execute(
        "INSERT INTO api_tokens (name, value, refreshed_at, expires_at) VALUES (?, ?, ?, ?)",
        (TOKEN_NAME, value, now.isoformat(), expires.isoformat()),
    )
    conn.commit()


def fetch_profile(token: str, http_get=_http_get) -> dict:
    """トークンで自分のアカウント情報を引く。疎通確認を兼ねる。"""
    return http_get(f"{GRAPH_BASE}/me", {"fields": "user_id,username", "access_token": token})


def refresh_token(
    conn: DbConnection, http_get=_http_get, now: datetime | None = None
) -> str:
    """必要ならトークンを更新する。返り値は結果の種別。

      no_token  … 未設定。何もしない（投稿機能を使っていない環境で正常）
      too_new   … 取得から24時間未満で、まだ更新できない
      expired   … 失効済み。更新では戻せず、再認可が要る
      refreshed … 更新した（また60日になった）
    """
    now = now or datetime.now(UTC)
    record = load_token(conn)
    if record is None:
        return "no_token"
    if record.days_left(now) <= 0:
        return "expired"
    if record.age(now) < MIN_AGE_FOR_REFRESH:
        return "too_new"

    body = http_get(
        f"{GRAPH_BASE}/refresh_access_token",
        {"grant_type": "ig_refresh_token", "access_token": record.value},
    )
    new_value = body.get("access_token")
    if not new_value:
        raise InstagramError(f"更新の応答に access_token がありません: {body}")
    save_token(conn, new_value, lifetime_sec=body.get("expires_in"), now=now)
    log.info("Instagram のトークンを更新しました（残り %d 日に戻りました）",
             int(timedelta(seconds=body.get("expires_in") or 0).days or 60))
    return "refreshed"


__all__ = [
    "GRAPH_BASE",
    "InstagramError",
    "TokenRecord",
    "fetch_profile",
    "load_token",
    "refresh_token",
    "save_token",
]


# ----------------------------------------------------------------------
# 認可コードの引き換え。
#
# ダッシュボードの「Generate token」は、沢田の画面で @frmg.jpn の管理者に
# ログインしてもらう形になる（画面共有か同席）。そこを避けたいときの経路。
#
#   1. authorization_url() のURLを管理者に送る
#   2. 管理者は自分の端末で開いて「許可」を押す
#   3. 審査UIの /ig/callback に着地し、code が画面に出る
#   4. その code を沢田が受け取り、この関数で長期トークンに換える
#
# 交換には app secret が要る。だから公開ホストではなく手元で叩く。

OAUTH_AUTHORIZE = "https://www.instagram.com/oauth/authorize"
OAUTH_ACCESS_TOKEN = "https://api.instagram.com/oauth/access_token"
# 投稿に要る最小のスコープ。basic はアカウント情報の読み取りで、
# content_publish が投稿。メッセージやコメントの権限は求めない。
SCOPES = ("instagram_business_basic", "instagram_business_content_publish")


def authorization_url(app_id: str, redirect_uri: str) -> str:
    """管理者に送る認可URL。"""
    from urllib.parse import urlencode

    return f"{OAUTH_AUTHORIZE}?" + urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(SCOPES),
        }
    )


def _http_post(url: str, data: dict) -> dict:
    import httpx

    response = httpx.post(url, data=data, timeout=30)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        error = body.get("error_message") or body.get("error", {})
        if isinstance(error, dict):
            error = error.get("message") or ""
        raise InstagramError(
            f"Instagram が {response.status_code} を返しました: {error or response.text[:200]}"
        )
    return body


def clean_code(raw: str) -> str:
    """人から受け取った文字列から、実際の認可コードだけを取り出す。

    ここを素通しにすると Meta は一律 `Invalid authorization code` を返し、
    「期限切れ」なのか「余計な文字が混ざっている」のか切り分けられない。
    実際に踏んだ／踏みうる混入は3つ:

      - **末尾の `#_`** … Meta がリダイレクトURLに必ず付ける。ドキュメントにも
        「使う前に取り除くこと」と明記がある
      - **コールバックURLごと** … 「画面のURLを送ってください」と受け取られると
        https://.../ig/callback?code=XXXX の形で届く
      - 前後の空白・改行 … チャットやメールの折り返しで付く
    """
    text = (raw or "").strip()
    if "code=" in text:
        # URLごと渡された場合。fragment(#_)より前の code= を拾う。
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(text).query)
        if query.get("code"):
            text = query["code"][0]
    # 断片指定が残っていれば落とす。`#_` に限らず `#` 以降は code ではない。
    text = text.split("#", 1)[0]
    return text.strip()


def exchange_code(
    code: str,
    app_id: str,
    app_secret: str,
    redirect_uri: str,
    http_post=_http_post,
    http_get=_http_get,
) -> str:
    """認可コードを長期トークン（60日）に換える。

    2段構えなのは Meta の仕様。まず短期トークン（1時間）を受け取り、
    それを長期トークンに交換する。片方だけでは投稿を続けられない。

    code は一度きり・短時間で失効する。失敗したら管理者にもう一度
    リンクを開いてもらう（同じURLで構わない）。

    受け取った文字列は clean_code を通す。URLごと渡されたり末尾に `#_` が
    付いていたりしても、Meta の返す `Invalid authorization code` は同じ
    文言なので、入力側で潰しておかないと切り分けができない。
    """
    code = clean_code(code)
    if not code:
        raise InstagramError("認可コードが空です。")

    short = http_post(
        OAUTH_ACCESS_TOKEN,
        {
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    short_token = short.get("access_token")
    if not short_token:
        raise InstagramError(f"短期トークンが返っていません: {short}")

    long_body = http_get(
        f"{GRAPH_BASE}/access_token",
        {
            "grant_type": "ig_exchange_token",
            "client_secret": app_secret,
            "access_token": short_token,
        },
    )
    long_token = long_body.get("access_token")
    if not long_token:
        raise InstagramError(f"長期トークンが返っていません: {long_body}")
    return long_token
