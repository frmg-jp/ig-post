"""公開ホスティング用の ASGI エントリポイント。

担当者と一緒に審査するために、審査UIをブラウザから開ける場所に置く。

    uvicorn freming.web.asgi:app --host 0.0.0.0 --port $PORT

`serve` との違いは2つ:

- **資格情報が無ければ起動しない。** REVIEW_UI_USER と REVIEW_UI_PASSWORD
  を要求する。認証なしで外向けに立ち上がる経路を作らないため
- **納品しない。** delivery.auto を落とす。理由は2つあって、
  1つは Drive の鍵を公開ホストに置かずに済ませるため（GitHub Actions で
  納品しないのと同じ方針）。もう1つは、納品ワーカーを2箇所で動かすと
  同じ物件を二重に納品するため。deliver.py の「納品済みか」の確認から
  Drive への書き込みまでには隙間があり、フォルダ名も「既存の最大値＋1」
  で決めているので、両方が同じ frmg_igNNN を取りに行く。
  **納品は手元の Mac の serve に一本化する。**

承認はDB（Neon）に入るので、手元で serve を上げれば、そこのワーカーが
公開側で承認されたものを拾って納品する。
"""

from __future__ import annotations

from freming.config import load_config
from freming.logging_setup import setup_logging
from freming.web.app import create_app
from freming.web.auth import credentials_from_env


def build_app():
    config = load_config()
    setup_logging(config.app.log_dir, config.app.log_level)

    auth = credentials_from_env()
    if auth is None:
        raise RuntimeError(
            "REVIEW_UI_USER と REVIEW_UI_PASSWORD が未設定です。"
            "公開して使う経路なので、認証なしでは起動しません"
        )

    # 納品はここではやらない（上の説明を参照）。
    config.delivery.auto = False
    return create_app(config, auth=auth)


app = build_app()
