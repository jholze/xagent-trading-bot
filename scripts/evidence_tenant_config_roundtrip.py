#!/usr/bin/env python3
"""Evidence script for tenant config roundtrip under real shipped get_config.

Runs under tenant_context + isolated pytest DB only. Prints full dict.
"""
import os
import sys
from pathlib import Path

# Make root importable (like tests/conftest)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Ensure isolated test DB
os.environ.setdefault("MONGODB_DB", "xagent_pytest")
os.environ.setdefault("PYTEST_RUNNING", "1")

from storage.mongo_client import drop_database
from core.tenant_context import tenant_context
from unittest.mock import patch
from data_manager import save_config, load_config, get_config
from storage.tenant_registry import create_tenant, get_gate_credentials

def main():
    drop_database(test=True)
    print("DB dropped for clean evidence run")

    # Force mongo backend for this evidence (matches test forcing)
    with patch("data_manager._should_use_mongo_for_tenant_config", return_value=True):
        create_tenant(
            "t_evidence",
            plan="pro",
            gate_api_key="EVIDENCE_KEY",
            gate_api_secret="EVIDENCE_SECRET",
            test=True,
        )
        print("Tenant created with Gate creds")

        with tenant_context("t_evidence", scope="paper"):
            save_config({"delegated": 42, "virtual_trading": True, "evidence": "full_body"})
            c = get_config()
            print("=== FULL get_config() UNDER CTX ===")
            print(repr(c))
            print("=== END ===")
            c2 = load_config()
            print("load_config also returned delegated?", c2.get("delegated"))
            print("get_gate under ctx:", get_gate_credentials("t_evidence", test=True))

    drop_database(test=True)
    print("Evidence run complete")

if __name__ == "__main__":
    main()
