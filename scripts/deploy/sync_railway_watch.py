#!/usr/bin/env python3
"""Apply deploy/railway/watch-patterns.json to Railway env `test` only.

Default is dry-run. Pass --apply to write. Never touches xagent-test / prod.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "deploy" / "railway" / "watch-patterns.json"


def _load() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _merged_patterns(spec: dict, svc: dict) -> list[str]:
    shared = list(spec.get("shared") or [])
    own = list(svc.get("patterns") or [])
    out: list[str] = []
    for p in shared + own:
        if p not in out:
            out.append(p)
    return out


def _payload(spec: dict) -> dict:
    services = {}
    skip = set(spec.get("do_not_touch") or [])
    for name, svc in (spec.get("services") or {}).items():
        if name in skip:
            raise SystemExit(f"refusing to configure do_not_touch service {name}")
        sid = svc.get("id")
        if not sid:
            raise SystemExit(f"missing railway id for {name}")
        services[sid] = {"build": {"watchPatterns": _merged_patterns(spec, svc)}}
    return {"services": services}


def _railway(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["railway", *args]
    return subprocess.run(
        cmd,
        input=stdin,
        text=True,
        capture_output=True,
        cwd=str(ROOT),
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit patterns to Railway test")
    parser.add_argument("--verify", action="store_true", help="read back test env after apply")
    args = parser.parse_args(argv)

    spec = _load()
    env = spec.get("environment") or "test"
    if env != "test":
        print("refusing: spec environment is not test", file=sys.stderr)
        return 2

    body = _payload(spec)
    print(f"spec {SPEC_PATH.relative_to(ROOT)}")
    print(f"environment {env}  services {len(body['services'])}  apply={args.apply}")
    for name, svc in spec["services"].items():
        pats = _merged_patterns(spec, svc)
        print(f"  {name:24} {len(pats)} patterns")

    if not args.apply:
        print("dry-run (pass --apply to write)")
        return 0

    msg = "test: sync sidecar watchPatterns from deploy/railway/watch-patterns.json"
    proc = _railway(
        ["environment", "edit", "--environment", env, "--message", msg, "--json"],
        stdin=json.dumps(body),
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout or "railway environment edit failed\n")
        return proc.returncode
    print((proc.stdout or "").strip() or "applied")

    if args.verify:
        proc = _railway(["environment", "config", "--environment", env, "--json"])
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr or "verify failed\n")
            return proc.returncode
        cfg = json.loads(proc.stdout)
        live = cfg.get("services") or {}
        bad = 0
        for name, svc in spec["services"].items():
            want = _merged_patterns(spec, svc)
            got = ((live.get(svc["id"]) or {}).get("build") or {}).get("watchPatterns") or []
            if list(got) != want:
                bad += 1
                print(f"MISMATCH {name}\n  want {want}\n  got  {got}")
            else:
                print(f"ok {name}")
        test_bot = live.get("0ad364f2-ea33-4b7c-ba50-b71a71a87711") or {}
        bot_wp = (test_bot.get("build") or {}).get("watchPatterns")
        if bot_wp:
            print(f"WARN xagent-test unexpectedly has watchPatterns={bot_wp}")
            bad += 1
        else:
            print("ok xagent-test has no watchPatterns")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
