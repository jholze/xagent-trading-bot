"""In-process client: same surface as DcaSniperBotClient without HTTP."""

from __future__ import annotations

from typing import Any


class LocalBotClient:
    """Calls bot_http builders/executors directly (no network)."""

    def cash(self) -> dict[str, Any]:
        from services.dca_sniper.bot_http import snapshot_cash, list_fund_winners

        body = {"ok": True, **snapshot_cash()}
        body["winners"] = list_fund_winners()
        return body

    def candidates(self) -> dict[str, Any]:
        from services.dca_sniper.bot_http import build_candidates

        cands = build_candidates()
        return {"ok": True, "candidates": cands, "n": len(cands)}

    def status(self) -> dict[str, Any]:
        from services.dca_sniper.config import dca_sniper_config, dca_sniper_enabled

        cfg = dca_sniper_config()
        holds = 0
        try:
            from strategies.positions import list_active_positions

            for p in list_active_positions():
                if p.get("recovery_hold") or p.get("sniper_focus"):
                    holds += 1
        except Exception:
            pass
        return {
            "ok": True,
            "enabled": dca_sniper_enabled(),
            "open_focus_holds": holds,
            "config": {
                "max_focus_slots": cfg["max_focus_slots"],
                "exclude_grid": cfg["exclude_grid"],
            },
        }

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        from services.dca_sniper.bot_http import execute_sniper_dca

        body, _status = execute_sniper_dca(payload)
        return body

    def fund_sell(self, payload: dict[str, Any]) -> dict[str, Any]:
        from services.dca_sniper.bot_http import execute_fund_sell

        body, _status = execute_fund_sell(payload)
        return body

    def promote(self, payload: dict[str, Any]) -> dict[str, Any]:
        from services.dca_sniper.bot_http import promote_position

        body, _status = promote_position(payload)
        return body
