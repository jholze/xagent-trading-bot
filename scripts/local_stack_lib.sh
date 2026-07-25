# Shared helpers for local_stack_*.sh — source only, do not exec.
# shellcheck shell=bash

_LOCAL_STACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_LOCAL_STACK_COMPOSE="${_LOCAL_STACK_ROOT}/deploy/local/docker-compose.yml"
_LOCAL_STACK_ENV="${_LOCAL_STACK_ROOT}/deploy/local/env.stack"
_LOCAL_STACK_ENV_EXAMPLE="${_LOCAL_STACK_ROOT}/deploy/local/env.stack.example"
_LOCAL_STACK_RUN_DIR="${_LOCAL_STACK_ROOT}/run/local_stack"

local_stack_root() {
  echo "${_LOCAL_STACK_ROOT}"
}

local_stack_compose_file() {
  echo "${_LOCAL_STACK_COMPOSE}"
}

local_stack_run_dir() {
  mkdir -p "${_LOCAL_STACK_RUN_DIR}"
  echo "${_LOCAL_STACK_RUN_DIR}"
}

local_stack_load_env() {
  # Safe local mongo first (clears Railway MONGO_URL)
  # shellcheck disable=SC1091
  source "${_LOCAL_STACK_ROOT}/scripts/dev_local_mongo.sh"

  if [[ -f "${_LOCAL_STACK_ENV}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${_LOCAL_STACK_ENV}"
    set +a
    echo "Loaded ${_LOCAL_STACK_ENV}"
  elif [[ -f "${_LOCAL_STACK_ENV_EXAMPLE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${_LOCAL_STACK_ENV_EXAMPLE}"
    set +a
    echo "Loaded ${_LOCAL_STACK_ENV_EXAMPLE} (copy to env.stack to customize)"
  fi

  # Re-assert local mongo after env file (env.stack must not reintroduce MONGO_URL)
  unset MONGO_URL
  export MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27017}"
  export MONGODB_DB="${MONGODB_DB:-xagent_test}"
  export MONGODB_TEST_DB="${MONGODB_TEST_DB:-xagent_pytest}"
  export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
  export WEAVIATE_URL="${WEAVIATE_URL:-http://127.0.0.1:8080}"
  export BOT_STACK="${BOT_STACK:-local}"
  export DEMO_MODE="${DEMO_MODE:-1}"
  export DEMO_LEDGER_BACKEND="${DEMO_LEDGER_BACKEND:-mongo}"
  export BOT_PORT="${BOT_PORT:-5000}"
  export HERMES_PORT="${HERMES_PORT:-8090}"
  export SANTIMENT_PORT="${SANTIMENT_PORT:-8091}"
  export MARKET_ORACLE_PORT="${MARKET_ORACLE_PORT:-8092}"
  export MEMORY_CORTEX_PORT="${MEMORY_CORTEX_PORT:-8765}"
  export HERMES_RUN_LEARNING="${HERMES_RUN_LEARNING:-0}"
}

local_stack_compose() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found"
    return 1
  fi
  docker compose -f "${_LOCAL_STACK_COMPOSE}" "$@"
}

local_stack_write_pid() {
  local name="$1"
  local pid="$2"
  local dir
  dir="$(local_stack_run_dir)"
  echo "${pid}" >"${dir}/${name}.pid"
  echo "${pid}"
}

local_stack_read_pid() {
  local name="$1"
  local f
  f="$(local_stack_run_dir)/${name}.pid"
  if [[ -f "$f" ]]; then
    cat "$f"
  fi
}

local_stack_stop_pidfile() {
  local name="$1"
  local f pid
  f="$(local_stack_run_dir)/${name}.pid"
  if [[ ! -f "$f" ]]; then
    return 0
  fi
  pid="$(cat "$f" 2>/dev/null || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    echo "Stopping ${name} (pid ${pid})..."
    kill "${pid}" 2>/dev/null || true
    sleep 0.5
    kill -9 "${pid}" 2>/dev/null || true
  fi
  rm -f "$f"
}

local_stack_http_ok() {
  local url="$1"
  curl -sf -m 3 "${url}" >/dev/null 2>&1
}

local_stack_python() {
  # Prefer a Python that has project deps (pymongo/pytest). Homebrew python3.14 often does not.
  local candidates=()
  if [[ -n "${LOCAL_STACK_PYTHON:-}" ]]; then
    candidates+=("${LOCAL_STACK_PYTHON}")
  fi
  candidates+=(
    python3.13
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
    /usr/local/bin/python3.13
    /opt/homebrew/bin/python3.13
    python3
  )
  local c
  for c in "${candidates[@]}"; do
    if command -v "$c" >/dev/null 2>&1 || [[ -x "$c" ]]; then
      if "$c" -c "import pymongo" >/dev/null 2>&1; then
        echo "$c"
        return 0
      fi
    fi
  done
  # Last resort: whatever python3 is
  command -v python3
}

local_stack_mongo_ok() {
  # Prefer tools that do not require project Python deps (pymongo).
  if command -v mongosh >/dev/null 2>&1; then
    mongosh --quiet "${MONGODB_URI:-mongodb://127.0.0.1:27017}" --eval 'db.adminCommand({ping:1}).ok' 2>/dev/null | grep -q 1 && return 0
  fi
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'xagent-local-mongo'; then
    docker exec xagent-local-mongo mongosh --quiet --eval 'db.adminCommand({ping:1}).ok' 2>/dev/null | grep -q 1 && return 0
  fi
  # Fallback: project python with pymongo
  local py
  py="$(local_stack_python)"
  if "$py" -c "from storage.mongo_client import ping_database; raise SystemExit(0 if ping_database() else 1)" 2>/dev/null; then
    return 0
  fi
  return 1
}
