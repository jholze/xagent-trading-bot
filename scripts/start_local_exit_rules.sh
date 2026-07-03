#!/usr/bin/env bash
# Step 1: verify tests + start local demo bot with new exit rules.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Step 1: Local Exit Rules ==="
bash scripts/safe_pytest.sh \
  tests/unit/test_trailing_take_profit.py \
  tests/unit/test_profit_max_lifetime.py \
  tests/unit/test_ohlcv_pagination.py \
  tests/unit/test_trailing_stop.py \
  -q

echo ""
echo "Open positions preview (local Mongo):"
# shellcheck disable=SC1091
source scripts/dev_local_mongo.sh
python3 scripts/open_positions_exit_preview.py

echo ""
echo "Starting dev bot (demo + ngrok)..."
exec bash scripts/start_demo_with_ngrok.sh