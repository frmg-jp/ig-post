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
