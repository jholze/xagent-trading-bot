from core.config import BotConfig, get_bot_config
from execution.gate_adapter import GateExecutionAdapter
from execution.paper_adapter import PaperExecutionAdapter
from services.portfolio_service import PortfolioService


def get_execution_adapter(config: BotConfig = None, portfolio: PortfolioService = None):
    cfg = config or get_bot_config()
    portfolio = portfolio or PortfolioService(cfg)
    if cfg.trading_mode == "live":
        return GateExecutionAdapter(cfg, portfolio)
    return PaperExecutionAdapter(portfolio)