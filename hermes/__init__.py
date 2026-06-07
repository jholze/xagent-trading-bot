"""Hermes self-improving trading agent."""

__all__ = ["HermesAgent"]


def __getattr__(name):
    if name == "HermesAgent":
        from hermes.agent import HermesAgent
        return HermesAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")