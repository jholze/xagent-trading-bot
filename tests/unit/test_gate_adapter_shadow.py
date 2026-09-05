"""GateExecutionAdapter shadow mode (#312): one execution path, no Paper adapter."""

from __future__ import annotations

import pytest

from core.config import BotConfig
from execution.factory import get_execution_adapter
from execution.gate_adapter import GateExecutionAdapter
from services.portfolio_service import PortfolioService


def test_paper_execution_adapter_module_gone():
    with pytest.raises(ImportError):
        from execution.paper_adapter import PaperExecutionAdapter  # noqa: F401


def test_factory_paper_builds_gate_shadow():
    cfg = BotConfig({"trading_mode": "paper", "live": {"execution": "real", "dry_run": False}})
    adapter = get_execution_adapter(cfg, PortfolioService(cfg))
    assert isinstance(adapter, GateExecutionAdapter)
    assert adapter.mode == "shadow"


def test_factory_demo_mode_live_real_raises(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("GATE_API_KEY", "k")
    monkeypatch.setenv("GATE_API_SECRET", "s")
    cfg = BotConfig(
        {
            "trading_mode": "live",
            "live_confirmed": True,
            "live": {"execution": "real", "dry_run": False},
        }
    )
    with pytest.raises(RuntimeError, match="DEMO_MODE"):
        get_execution_adapter(cfg, PortfolioService(cfg))
