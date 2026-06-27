#!/usr/bin/env python3
"""Run full verification-plan captures via subprocess; wipe SCRATCH each run."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SCRATCH = (
    "/var/folders/qg/50gj9bls3yq6tvjyx50zvlwc0000gp/T/"
    "grok-goal-23d611747fb8/implementer"
)
SCRATCH = Path(os.environ.get("SCRATCH", DEFAULT_SCRATCH))


def wipe_scratch() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for child in SCRATCH.iterdir():
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)


def run_capture(cmd: list[str], out_name: str, *, env: dict | None = None) -> int:
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    out_path = SCRATCH / out_name
    out_path.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(f"EXIT:{proc.returncode}\n")
    return proc.returncode


def assert_snapshot_ok(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return (
        "open_positions=25" in text
        and "[invariants-ok]" in text
        and "[migrate]" in text
        and "load_positions_keys=25" in text
        and "EXIT:0" in text
    )


def main() -> int:
    wipe_scratch()

    py = sys.executable
    base_env = {**os.environ}
    demo_test_env = {**base_env, "DEMO_MODE": "1", "MONGODB_DB": "xagent_test"}
    demo_prod_env = {**base_env, "DEMO_MODE": "1", "MONGODB_DB": "xagent"}

    run_capture(
        [
            py,
            "-c",
            "from services.demo_snapshot_report import strip_demo_test_pollution; print('stripped', strip_demo_test_pollution())",
        ],
        "strip_pollution.txt",
        env=demo_test_env,
    )

    for i in (1, 2):
        run_capture(
            [py, str(ROOT / "scripts" / "migrate_trades_to_orders.py")],
            f"migrate{i}.txt",
            env=demo_test_env,
        )
    shutil.copy2(SCRATCH / "migrate1.txt", SCRATCH / "migrate.txt")

    run_capture(
        [
            py,
            "-c",
            "from services.demo_snapshot_report import strip_demo_test_pollution; print('stripped', strip_demo_test_pollution())",
        ],
        "strip_pollution_post_migrate.txt",
        env=demo_test_env,
    )

    # Step 1: guard abort (demo + production db)
    run_capture(
        [
            py,
            "-c",
            (
                "import os\n"
                "os.environ['DEMO_MODE']='1'\n"
                "os.environ['MONGODB_DB']='xagent'\n"
                "from storage.mongo_client import assert_safe_demo_mongo_db\n"
                "assert_safe_demo_mongo_db()"
            ),
        ],
        "aria_guard_abort.txt",
        env=demo_prod_env,
    )

    # Step 1: guard ok + INFO log
    run_capture(
        [
            py,
            "-c",
            (
                "import os\n"
                "os.environ['DEMO_MODE']='1'\n"
                "os.environ['MONGODB_DB']='xagent_test'\n"
                "from storage.mongo_client import assert_safe_demo_mongo_db, log_ledger_startup\n"
                "db = assert_safe_demo_mongo_db()\n"
                "log_ledger_startup()\n"
                "print('guard_ok db=', db)"
            ),
        ],
        "aria_guard_ok.txt",
        env=demo_test_env,
    )

    for i in (1, 2):
        run_capture(
            [py, "-c", "import aria_bot"],
            f"aria_import_{i}.txt",
            env=demo_test_env,
        )

    rc_snapshot1 = run_capture(
        [py, str(ROOT / "scripts" / "mongo_snapshot_demo.py"), "--dry-run", "--test-db"],
        "snapshot_demo_run1.txt",
        env=demo_test_env,
    )
    rc_snapshot2 = run_capture(
        [py, str(ROOT / "scripts" / "mongo_snapshot_demo.py"), "--dry-run", "--test-db"],
        "snapshot_demo_run2.txt",
        env=demo_test_env,
    )
    shutil.copy2(SCRATCH / "snapshot_demo_run1.txt", SCRATCH / "snapshot_demo.txt")

    rc_apply = run_capture(
        [py, str(ROOT / "scripts" / "mongo_snapshot_demo.py"), "--test-db", "--no-json"],
        "snapshot_apply.txt",
        env=demo_test_env,
    )

    run_capture(
        [
            py,
            "-c",
            "from services.demo_snapshot_report import strip_demo_test_pollution; print('stripped', strip_demo_test_pollution())",
        ],
        "strip_pollution_post_apply.txt",
        env=demo_test_env,
    )

    # Step 2: phase1 tests + cycle writes
    rc_phase1 = []
    for i in (1, 2):
        rc_phase1.append(
            run_capture(
                [
                    py,
                    "-m",
                    "pytest",
                    "tests/unit/test_positions_fast.py",
                    "tests/unit/test_derive_positions.py",
                    "tests/unit/test_portfolio_equity.py",
                    "tests/unit/test_mongo_ledger.py",
                    "-q",
                    "--tb=line",
                ],
                f"phase1_tests_run{i}.txt",
            )
        )
    shutil.copy2(SCRATCH / "phase1_tests_run1.txt", SCRATCH / "phase1_tests.txt")

    rc_phase1_full = []
    for i in (1, 2):
        rc_phase1_full.append(
            run_capture(
                [py, "-m", "pytest", "tests/", "-q", "--tb=no"],
                f"phase1_full_tests_run{i}.txt",
            )
        )

    run_capture(
        [
            py,
            "-c",
            "from services.demo_snapshot_report import strip_demo_test_pollution; strip_demo_test_pollution()",
        ],
        "strip_pollution_pre_cycle.txt",
        env=demo_test_env,
    )

    cycle_env = {**demo_test_env, "CYCLE_WRITES_OUT": str(SCRATCH / "cycle_writes.log")}
    rc_cycle = run_capture(
        [py, str(ROOT / "scripts" / "cycle_writes_sim.py")],
        "cycle_writes_capture.txt",
        env=cycle_env,
    )

    # Step 3: router tests + loc + exercise
    rc_router = []
    for i in (1, 2):
        rc_router.append(
            run_capture(
                [
                    py,
                    "-m",
                    "pytest",
                    "tests/unit/",
                    "-k",
                    "mongo or ledger or router or data_manager",
                    "-q",
                    "--tb=line",
                ],
                f"router_tests_run{i}.txt",
            )
        )
    shutil.copy2(SCRATCH / "router_tests_run1.txt", SCRATCH / "router_tests.txt")

    rc_phase2_full = []
    for i in (1, 2):
        rc_phase2_full.append(
            run_capture(
                [py, "-m", "pytest", "tests/", "-q", "--tb=no"],
                f"phase2_full_tests_run{i}.txt",
            )
        )

    for i in (1, 2):
        run_capture(["wc", "-l", str(ROOT / "data_manager.py")], f"data_manager_loc_run{i}.txt")
    shutil.copy2(SCRATCH / "data_manager_loc_run1.txt", SCRATCH / "data_manager_loc.txt")

    run_capture(
        [
            py,
            "-c",
            (
                "from services.order_service import OrderService\n"
                "from strategies.positions import bootstrap_positions, load_positions\n"
                "from data_manager import get_config\n"
                "svc = OrderService(get_config())\n"
                "bootstrap_positions('demo')\n"
                "print('orders', len(svc.list_orders()))\n"
                "print('positions', len(load_positions('demo')))"
            ),
        ],
        "router_exercise.log",
        env=demo_test_env,
    )

    # Step 4: SOT + equity
    for i in (1, 2):
        run_capture(
            [
                py,
                "-c",
                (
                    "from services.ledger_sync import _build_positions_snapshot_from_orders, sync_positions_on_startup\n"
                    "from data_manager import load_orders, resolve_ledger_scope\n"
                    "print('orders derive exercised', resolve_ledger_scope('demo'), len(load_orders('demo').get('orders',[])))"
                ),
            ],
            f"sot_exercise_run{i}.txt",
            env=demo_test_env,
        )
    shutil.copy2(SCRATCH / "sot_exercise_run1.txt", SCRATCH / "sot_exercise.txt")

    rc_phase3_full = []
    for i in (1, 2):
        rc_phase3_full.append(
            run_capture(
                [py, "-m", "pytest", "tests/", "-q", "--tb=no"],
                f"phase3_full_tests_run{i}.txt",
            )
        )

    for i in (1, 2):
        run_capture(
            [
                py,
                "-c",
                "from notifications.daily_portfolio import estimate_nav_at_day_start; print(estimate_nav_at_day_start('demo'))",
            ],
            f"equity_run{i}.txt",
            env=demo_test_env,
        )
    shutil.copy2(SCRATCH / "equity_run1.txt", SCRATCH / "equity.txt")

    run_capture(
        [
            py,
            "-c",
            (
                "from notifications.daily_portfolio import estimate_nav_at_day_start\n"
                "from notifications.terminal_dashboard import _portfolio_snapshot\n"
                "from strategies.positions import bootstrap_positions, count_open_positions, load_positions\n"
                "bootstrap_positions('demo')\n"
                "snap = _portfolio_snapshot('demo')\n"
                "nav_start = estimate_nav_at_day_start('demo')\n"
                "total = float(snap.get('total_value', 0) or 0)\n"
                "delta = total - nav_start\n"
                "print(f'scope=demo open_positions={count_open_positions()}')\n"
                "print(f'load_positions_returned={len(load_positions(\"demo\"))}')\n"
                "print(f'total_value=${total:,.2f}')\n"
                "print(f'nav_day_start=${nav_start:,.2f}')\n"
                "print(f'nav_delta=${delta:,.2f}')\n"
                "print(f'nav_delta_under_2k={abs(delta) < 2000}')"
            ),
        ],
        "equity_demo.txt",
        env=demo_test_env,
    )

    # Step 5: baseline + capital tests
    for i in (1, 2):
        run_capture(
            [
                py,
                "-c",
                (
                    "from core import portfolio_baseline\n"
                    "from risk.risk_manager import RiskManager\n"
                    "from notifications.telegram_commands.position_display import format_portfolio_summary\n"
                    "from notifications.daily_portfolio import estimate_nav_at_day_start\n"
                    "print('baseline exercised', portfolio_baseline.initial_capital(scope='demo'))"
                ),
            ],
            f"baseline_run{i}.txt",
            env=demo_test_env,
        )
    shutil.copy2(SCRATCH / "baseline_run1.txt", SCRATCH / "baseline.txt")

    rc_capital = []
    for i in (1, 2):
        rc_capital.append(
            run_capture(
                [
                    py,
                    "-m",
                    "pytest",
                    "tests/unit/test_daily_portfolio.py",
                    "tests/unit/test_position_display.py",
                    "tests/unit/test_portfolio_equity.py",
                    "-q",
                    "--tb=line",
                ],
                f"capital_tests_run{i}.txt",
            )
        )
    shutil.copy2(SCRATCH / "capital_tests_run1.txt", SCRATCH / "capital_tests.txt")

    rc_phase4_full = []
    for i in (1, 2):
        rc_phase4_full.append(
            run_capture(
                [py, "-m", "pytest", "tests/", "-q", "--tb=no"],
                f"phase4_full_tests_run{i}.txt",
            )
        )

    # Step 6: full tests + entry twice
    rc_tests = []
    for i in (1, 2):
        rc_tests.append(
            run_capture([py, "-m", "pytest", "tests/", "-q", "--tb=no"], f"full_tests_run{i}.txt")
        )

    run_capture(
        [
            py,
            "-c",
            "from services.demo_snapshot_report import strip_demo_test_pollution; strip_demo_test_pollution()",
        ],
        "strip_pollution_pre_entry.txt",
        env=demo_test_env,
    )

    entry_snippet = (
        "import aria_bot\n"
        "from strategies.positions import load_positions, save_positions, update_position, count_open_positions\n"
        "from services.order_service import OrderService\n"
        "print('entry exercised, open=', count_open_positions())\n"
        "print('load_positions keys=', len(load_positions('demo')))\n"
        "print('OrderService ok', OrderService is not None)"
    )
    rc_entry = []
    for i in (1, 2):
        rc_entry.append(
            run_capture([py, "-c", entry_snippet], f"entry_run{i}.txt", env=demo_test_env)
        )
    shutil.copy2(SCRATCH / "entry_run1.txt", SCRATCH / "entry.txt")

    run_capture(
        [
            py,
            "-c",
            (
                "import pathlib\n"
                "root = pathlib.Path('.')\n"
                "for rel in ('aria_bot.py', 'services/ledger_sync.py'):\n"
                "    text = (root / rel).read_text(encoding='utf-8')\n"
                "    hits = [i+1 for i,l in enumerate(text.splitlines()) if 'backfill_orders_from_trade_history' in l]\n"
                "    print(rel, 'hits=', hits or 'none')"
            ),
        ],
        "grep_startup.txt",
        env=base_env,
    )

    bad_26 = []
    for path in SCRATCH.iterdir():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"open[= _]26|from_orders=26", text):
            bad_26.append(path.name)

    guard_abort = (SCRATCH / "aria_guard_abort.txt").read_text(encoding="utf-8")
    guard_ok = (SCRATCH / "aria_guard_ok.txt").read_text(encoding="utf-8")
    cycle_log = (SCRATCH / "cycle_writes.log").read_text(encoding="utf-8") if (SCRATCH / "cycle_writes.log").exists() else ""
    apply_txt = (SCRATCH / "snapshot_apply.txt").read_text(encoding="utf-8")

    snapshot1_txt = (SCRATCH / "snapshot_demo_run1.txt").read_text(encoding="utf-8")
    ok = (
        "Demo mode refuses production MongoDB database" in guard_abort
        and "scope=" in guard_ok
        and "db=" in guard_ok
        and rc_snapshot1 == 0
        and rc_snapshot2 == 0
        and assert_snapshot_ok(SCRATCH / "snapshot_demo_run1.txt")
        and assert_snapshot_ok(SCRATCH / "snapshot_demo_run2.txt")
        and "[migrate]" in snapshot1_txt
        and "[migrate]" in apply_txt
        and rc_apply == 0
        and "zero_writes=True" in cycle_log
        and rc_cycle == 0
        and all(r == 0 for r in rc_tests)
        and all(r == 0 for r in rc_entry)
        and all(r == 0 for r in rc_phase1)
        and all(r == 0 for r in rc_phase1_full)
        and all(r == 0 for r in rc_router)
        and all(r == 0 for r in rc_phase2_full)
        and all(r == 0 for r in rc_phase3_full)
        and all(r == 0 for r in rc_capital)
        and all(r == 0 for r in rc_phase4_full)
        and not bad_26
    )

    (SCRATCH / "capture_summary.txt").write_text(
        "\n".join(
            [
                f"guard_abort_ok={'Demo mode refuses production MongoDB database' in guard_abort}",
                f"guard_ok_info={'scope=' in guard_ok and 'db=' in guard_ok}",
                f"snapshot_run1_ok={assert_snapshot_ok(SCRATCH / 'snapshot_demo_run1.txt')}",
                f"snapshot_run2_ok={assert_snapshot_ok(SCRATCH / 'snapshot_demo_run2.txt')}",
                f"snapshot_dry_migrate_ok={'[migrate]' in snapshot1_txt}",
                f"apply_migrate_ok={'[migrate]' in apply_txt}",
                f"phase1_full_ok={all(r == 0 for r in rc_phase1_full)}",
                f"phase2_full_ok={all(r == 0 for r in rc_phase2_full)}",
                f"phase3_full_ok={all(r == 0 for r in rc_phase3_full)}",
                f"phase4_full_ok={all(r == 0 for r in rc_phase4_full)}",
                f"cycle_zero_writes={'zero_writes=True' in cycle_log}",
                f"bad_26_files={bad_26 or 'none'}",
                f"all_ok={ok}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"evidence written to {SCRATCH}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())