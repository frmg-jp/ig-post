#!/usr/bin/env bash
# =====================================================================
# 有効な全ソースの配信ペースを測る。
#
# 「10件」はフィードの窓であって1日分ではない。それが何日分なのかを
# 測らないと、審査に上がる件数の見積もりが桁で外れる。
#
# DBには一切書き込みません（probe は dry-run）。
# フィード1回ずつのリクエストしか送りません。所要は1分ほど。
#
# 使い方:
#   bash scripts/measure_pace.sh
# =====================================================================
set -uo pipefail

FEEDS=$(python - <<'PY'
from freming.config import load_config
for s in load_config("config.yaml").editorial_sources:
    if s.enabled:
        for f in s.feeds:
            print(f)
PY
)

# shellcheck disable=SC2086
python -m freming.cli probe-feed $FEEDS

echo
echo "各行の「審査 週N件」を足したものが、1週間に審査へ上がる件数の見込みです。"
