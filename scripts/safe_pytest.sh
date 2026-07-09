#!/usr/bin/env bash
# Run pytest against localhost Mongo only — safe even if MONGO_URL is set in the shell.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/dev_local_mongo.sh
source "${ROOT}/scripts/dev_local_mongo.sh"
cd "${ROOT}"
exec python3 -m pytest "$@"