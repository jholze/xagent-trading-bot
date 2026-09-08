from core.config import BotConfig, get_bot_config
from core.execution_mode import resolve_execution_mode
from execution.gate_adapter import GateExecutionAdapter
from services.portfolio_service import PortfolioService


def get_execution_adapter(config: BotConfig = None, portfolio: PortfolioService = None):
    cfg = config or get_bot_config()
    portfolio = portfolio or PortfolioService(cfg)
    resolved = resolve_execution_mode(cfg.raw)
    return GateExecutionAdapter(cfg, portfolio, mode=resolved.adapter_mode)