from services.signal_orchestrator import SignalOrchestrator
from strategies.technical_rsi_bb import get_ampel_color

_orchestrator = None


def _get_orchestrator(notify_callback=None) -> SignalOrchestrator:
    global _orchestrator
    if _orchestrator is None or notify_callback is not None:
        _orchestrator = SignalOrchestrator(notify_callback=notify_callback)
    return _orchestrator


def check_signal(coin, current_price, x_signals=None, notify_callback=None):
    """Backward-compatible facade — delegates to SignalOrchestrator."""
    if notify_callback is None:
        try:
            from telegram_notifier import send_signal_message
            notify_callback = send_signal_message
        except ImportError:
            notify_callback = None
    return _get_orchestrator(notify_callback).process_coin(coin, current_price, x_signals)