#!/usr/bin/env bash
# X / Twitter pipeline health check → Terminal + optional Telegram summary
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/x_health_check.py "$@"