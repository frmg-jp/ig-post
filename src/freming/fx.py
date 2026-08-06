"""円換算レートの取得と保管。

審査UIで価格を通貨をまたいで並べるためだけに使う。表示には使わない。

    fx update（定期実行が毎日呼ぶ） → 7日より古ければ取得 → DBに保管

**config.yaml ではなくDBに置く。** api_tokens と同じ理由で、定期的に
書き換わる値だから。config.yaml は git 管理下にあり、自動更新するには
毎回コミットとプッシュが要る。DBなら行を書き換えるだけで済み、審査UI
（Render）と手元が同じ値を見る。

config.yaml の fx.jpy_per は土台として残す。取得が一度も成功していない
環境（新しい clone、テスト）でも並べ替えが動くようにするため。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from freming.config import Config
from freming.db.connection import DbConnection
from freming.logging_setup import get_logger

log = get_logger(__name__)

# 鍵が要らず、TWD を含む166通貨を返す。base を JPY にすると
# 「1円あたりの各通貨」が返るので、逆数を取って「1通貨あたりの円」にする。
RATES_URL = "https://open.er-api.com/v6/latest/JPY"
# 既定の更新間隔。定期実行は毎日呼ぶが、これより新しければ何もしない。
# 週1回にしたいが、専用のワークフローを足すより「毎日呼んで7日で判定」の
# ほうが、1回落ちても翌日やり直せる。
DEFAULT_MAX_AGE = timedelta(days=7)


class FxError(RuntimeError):
    """レートを取得できなかった。"""


def _http_get(url: str) -> dict:
    import httpx

    response = httpx.get(url, timeout=30)
    if response.status_code != 200:
        raise FxError(f"{url} が {response.status_code} を返しました")
    try:
        return response.json()
    except ValueError as exc:
        raise FxError(f"応答がJSONではありません: {exc}") from exc


def fetch_rates(currencies: list[str], http_get=_http_get) -> dict[str, float]:
    """1通貨あたりの円を返す。

    提供元は「1円あたりの各通貨」を返すので逆数を取る。0 や負の値は
    計算できないので落とす（並べ替えが壊れるより、その通貨だけ
    末尾に回るほうがよい）。
    """
    body = http_get(RATES_URL)
    rates = body.get("rates") or {}
    if not rates:
        raise FxError(f"rates が空です: {list(body)[:6]}")

    out: dict[str, float] = {}
    for code in currencies:
        if code == "JPY":
            out[code] = 1.0
            continue
        per_jpy = rates.get(code)
        if not isinstance(per_jpy, int | float) or per_jpy <= 0:
            log.warning("レートを取得できません: %s", code)
            continue
        out[code] = round(1.0 / per_jpy, 4)
    if not out:
        raise FxError("必要な通貨のレートが1つも取れませんでした")
    return out


def save_rates(conn: DbConnection, rates: dict[str, float], now: datetime | None = None) -> None:
    stamp = (now or datetime.now(UTC)).isoformat()
    for code, value in rates.items():
        # SQLite/Postgres 両対応の UPSERT。currency は PRIMARY KEY。
        conn.execute("DELETE FROM fx_rates WHERE currency = ?", (code,))
        conn.execute(
            "INSERT INTO fx_rates (currency, jpy_per, fetched_at) VALUES (?, ?, ?)",
            (code, float(value), stamp),
        )
    conn.commit()


def load_rates(conn: DbConnection) -> tuple[dict[str, float], str | None]:
    """保管しているレートと、その取得時刻を返す。無ければ ({}, None)。"""
    rows = conn.execute("SELECT currency, jpy_per, fetched_at FROM fx_rates").fetchall()
    if not rows:
        return ({}, None)
    rates = {r["currency"]: float(r["jpy_per"]) for r in rows}
    newest = max(r["fetched_at"] for r in rows)
    return (rates, newest)


def effective_rates(conn: DbConnection, config: Config) -> tuple[dict[str, float], str]:
    """実際に並べ替えに使うレートと、画面に出す基準日。

    DBに取得済みのものがあればそれを使い、無ければ config.yaml の値に
    落ちる。取得が一度も成功していない環境でも並べ替えは動く。
    """
    rates, fetched_at = load_rates(conn)
    if rates:
        return (rates, f"{fetched_at[:10]} 時点")
    return (dict(config.fx.jpy_per), config.fx.as_of)


def update_rates(
    conn: DbConnection,
    config: Config,
    *,
    max_age: timedelta = DEFAULT_MAX_AGE,
    force: bool = False,
    http_get=_http_get,
    now: datetime | None = None,
) -> str:
    """必要なら取得して保管する。返り値は結果の種別。

      fresh    … まだ新しいので何もしなかった
      updated  … 取得して保管した

    定期実行は毎日これを呼ぶ。実際に外へ出るのは max_age を過ぎたときだけ。
    """
    now = now or datetime.now(UTC)
    _, fetched_at = load_rates(conn)
    if not force and fetched_at:
        try:
            age = now - datetime.fromisoformat(fetched_at)
        except ValueError:
            age = max_age  # 読めない値は古いものとして扱い、取り直す
        if age < max_age:
            return "fresh"

    # 必要な通貨は config が持つ。収集対象が増えたらそこに足す。
    currencies = sorted(config.fx.jpy_per) or ["USD", "EUR", "GBP", "JPY"]
    save_rates(conn, fetch_rates(currencies, http_get=http_get), now=now)
    return "updated"
