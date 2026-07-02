#!/usr/bin/env bash
# Phase 0 multi-tenant verification — captures evidence to SCRATCH dir.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH="${SCRATCH:-/var/folders/qg/50gj9bls3yq6tvjyx50zvlwc0000gp/T/grok-goal-aa793b6de675/implementer}"
mkdir -p "$SCRATCH"
cd "$ROOT"

BOT_SNIPPET='import os
os.environ.pop("MONGODB_DB", None)
os.environ.setdefault("DEMO_MODE", "1")
import aria_bot
from data_manager import resolve_ledger_scope, load_orders, load_positions_document, get_config
print("ARIA_BOT_IMPORT_OK")
scope = resolve_ledger_scope()
print("DEFAULT_SCOPE:", scope)
cfg = get_config()
print("TRADING_MODE:", cfg.get("trading_mode"))
ords = load_orders(scope)
print("ORDERS_LEN:", len(ords.get("orders", [])))
print("ORDERS_LEDGER_SCOPE:", ords.get("ledger_scope"))
pos = load_positions_document(scope)
print("POSITIONS_KEYS:", len(pos.get("positions", {})))
print("BOT_PRIMARY_OBSERVABLES_OK")'

echo "=== Step 1: ledger tests x2 ==="
python3 -m pytest tests/unit/test_mongo_ledger.py tests/unit/test_mongo_backend.py \
  tests/unit/test_order_isolation.py tests/unit/test_positions_fast.py \
  tests/unit/test_demo_ledger_store.py tests/unit/test_ledger_sync.py \
  tests/unit/test_tenant_isolation.py -q --tb=line 2>&1 | tee "$SCRATCH/pre_ledger_tests1.txt"
python3 -m pytest tests/unit/test_mongo_ledger.py tests/unit/test_mongo_backend.py \
  tests/unit/test_order_isolation.py tests/unit/test_positions_fast.py \
  tests/unit/test_demo_ledger_store.py tests/unit/test_ledger_sync.py \
  tests/unit/test_tenant_isolation.py -q --tb=line 2>&1 | tee "$SCRATCH/pre_ledger_tests2.txt"

echo "=== Step 2: migration dry-run + apply ==="
python3 scripts/migrate_single_to_tenant.py --test --dry-run 2>&1 | tee "$SCRATCH/migrate_run1.txt"
python3 scripts/migrate_single_to_tenant.py --test --dry-run 2>&1 | tee "$SCRATCH/migrate_run2.txt"
python3 scripts/verify_tenant_phase0.py migrate 2>&1 | tee "$SCRATCH/migrate_apply.txt"

echo "=== Step 3: bot entry x2 (subprocess, stable observables) ==="
python3 -c "$BOT_SNIPPET" 2>&1 | tee "$SCRATCH/bot_entry1.txt"
python3 -c "$BOT_SNIPPET" 2>&1 | tee "$SCRATCH/bot_entry2.txt"

echo "=== Step 4: isolation exercise x2 ==="
python3 scripts/verify_tenant_phase0.py isolation 2>&1 | tee "$SCRATCH/isolation_ex1.txt"
python3 scripts/verify_tenant_phase0.py isolation 2>&1 | tee "$SCRATCH/isolation_ex2.txt"

echo "=== Step 6: router evidence ==="
python3 -c "
from storage.ledger_router import resolve_store
from data_manager import resolve_ledger_scope
s = resolve_store(resolve_ledger_scope())
print('STORE_TYPE:', type(s).__name__)
" 2>&1 | tee "$SCRATCH/router_evidence.txt"

echo "=== Step 5: full suite x2 ==="
python3 -m pytest tests/ -q --tb=no 2>&1 | tee "$SCRATCH/full_suite1.txt"
python3 -m pytest tests/ -q --tb=no 2>&1 | tee "$SCRATCH/full_suite2.txt"

echo "VERIFICATION_COMPLETE"