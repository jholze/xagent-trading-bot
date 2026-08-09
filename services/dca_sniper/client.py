"""HTTP client: sniper service → bot internal API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from services.dca_sniper.config import bot_base_url, internal_token


class DcaSniperBotClient:
    """HTTP client from standalone sniper → bot internal APIs only (ledger stays on bot)."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout_sec: float = 30.0,
    ):
        self.base = (base_url or bot_base_url()).rstrip("/")
        # strip accidental path suffixes
        for suffix in ("/internal/dca-sniper", "/internal"):
            if self.base.endswith(suffix):
                self.base = self.base[: -len(suffix)]
        self.token = token if token is not None else internal_token()
        self.timeout = timeout_sec

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "xagent-dca-sniper/1",
        }
        if self.token:
            h["X-Dca-Sniper-Token"] = self.token
        return h

    def _url(self, path: str) -> str:
        p = path if path.startswith("/") else f"/{path}"
        return f"{self.base}{p}"

    def _request(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._url(path),
            data=data,
            headers=self._headers(),
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                out = json.loads(raw) if raw else {}
                return out if isinstance(out, dict) else {"ok": False, "message": "bad_json"}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            return {
                "ok": False,
                "message": f"http_{e.code}",
                "detail": detail or str(e.reason),
            }
        except Exception as e:
            return {"ok": False, "message": f"error:{e}"[:200]}

    def candidates(self) -> dict[str, Any]:
        return self._request("GET", "/internal/dca-sniper/candidates")

    def cash(self) -> dict[str, Any]:
        return self._request("GET", "/internal/dca-sniper/cash")

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/internal/dca-sniper/status")

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/internal/dca-sniper/execute", payload)

    def fund_sell(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/internal/dca-sniper/fund-sell", payload)

    def promote(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/internal/dca-sniper/promote", payload)
