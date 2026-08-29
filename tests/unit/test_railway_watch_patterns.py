"""Watch-pattern spec must never filter the paper bot."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "deploy" / "railway" / "watch-patterns.json"


class TestRailwayWatchPatterns:
    def test_spec_skips_paper_bot_and_prod(self):
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        assert spec["environment"] == "test"
        skip = set(spec["do_not_touch"])
        assert "xagent-test" in skip
        assert "xagent-bot" in skip
        assert "xagent-test" not in spec["services"]
        assert "xagent-bot" not in spec["services"]

    def test_every_sidecar_includes_shared_build_files(self):
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        shared = spec["shared"]
        assert "scripts/railway_start.sh" in shared
        assert "Dockerfile" in shared
        assert "requirements.txt" in shared
        from scripts.deploy.sync_railway_watch import _merged_patterns

        for name, svc in spec["services"].items():
            merged = _merged_patterns(spec, svc)
            for item in shared:
                assert item in merged, f"{name} missing shared {item}"

    def test_dry_run_does_not_call_railway(self):
        from scripts.deploy.sync_railway_watch import main

        with patch("scripts.deploy.sync_railway_watch._railway") as rw:
            assert main([]) == 0
            rw.assert_not_called()
