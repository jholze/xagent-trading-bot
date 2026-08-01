"""Exit-radar sidecar: owner routing, remote execute, fire API auth."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


class TestExitRealtimeOwner(unittest.TestCase):
    def tearDown(self):
        for k in (
            "EXIT_REALTIME_OWNER",
            "RUN_EXIT_RADAR",
            "RAILWAY_SERVICE_NAME",
            "EXIT_EXECUTE_URL",
            "EXIT_WS_INTERNAL_TOKEN",
        ):
            os.environ.pop(k, None)

    def test_default_owner_bot(self):
        from services.exit_realtime.config import exit_realtime_owner

        os.environ.pop("EXIT_REALTIME_OWNER", None)
        self.assertEqual(exit_realtime_owner({"exit_realtime": {}}), "bot")

    def test_config_owner_sidecar(self):
        from services.exit_realtime.config import (
            exit_realtime_owner,
            exit_realtime_should_run_hub,
        )

        raw = {"exit_realtime": {"enabled": True, "mode": "live", "owner": "sidecar"}}
        self.assertEqual(exit_realtime_owner(raw), "sidecar")
        os.environ.pop("RUN_EXIT_RADAR", None)
        os.environ.pop("RAILWAY_SERVICE_NAME", None)
        self.assertFalse(exit_realtime_should_run_hub(raw))

    def test_bot_runs_hub_when_owner_bot(self):
        from services.exit_realtime.config import exit_realtime_should_run_hub

        raw = {"exit_realtime": {"enabled": True, "mode": "live", "owner": "bot"}}
        os.environ.pop("RUN_EXIT_RADAR", None)
        self.assertTrue(exit_realtime_should_run_hub(raw))

    def test_sidecar_process_runs_hub(self):
        from services.exit_realtime.config import (
            exit_realtime_should_run_hub,
            is_exit_radar_sidecar_process,
        )

        os.environ["RUN_EXIT_RADAR"] = "1"
        self.assertTrue(is_exit_radar_sidecar_process())
        raw = {"exit_realtime": {"enabled": True, "mode": "live", "owner": "bot"}}
        self.assertTrue(exit_realtime_should_run_hub(raw))

    def test_env_owner_overrides_config(self):
        from services.exit_realtime.config import exit_realtime_owner

        os.environ["EXIT_REALTIME_OWNER"] = "sidecar"
        raw = {"exit_realtime": {"owner": "bot"}}
        self.assertEqual(exit_realtime_owner(raw), "sidecar")


class TestRemoteExecute(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("EXIT_EXECUTE_URL", None)
        os.environ.pop("EXIT_WS_INTERNAL_TOKEN", None)

    def test_remote_path_posts_json(self):
        from services.exit_realtime.execute import try_execute_trail_exit

        os.environ["EXIT_EXECUTE_URL"] = "http://bot.example/internal/exit-ws/fire"
        os.environ["EXIT_WS_INTERNAL_TOKEN"] = "secret-token"

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"ok": True, "executed": True, "message": "ok"}
                ).encode()

        with patch(
            "services.exit_realtime.execute.urllib.request.urlopen",
            return_value=_Resp(),
        ) as mock_open:
            r = try_execute_trail_exit(
                symbol="TAG/USDT",
                timeframe="1h",
                price=1.23,
                action="SELL_FULL",
                exit_source="trailing_stop",
                rationale="test",
            )
        self.assertTrue(r["executed"])
        self.assertTrue(r.get("remote"))
        req = mock_open.call_args[0][0]
        self.assertEqual(req.get_header("X-exit-ws-token") or req.headers.get("X-Exit-Ws-Token"), "secret-token")

    def test_force_local_ignores_url(self):
        from services.exit_realtime.execute import try_execute_trail_exit

        os.environ["EXIT_EXECUTE_URL"] = "http://should-not-call"
        with patch(
            "strategies.positions.get_position",
            return_value={"amount": 0},
        ), patch(
            "strategies.positions.is_open_position",
            return_value=False,
        ):
            r = try_execute_trail_exit(
                symbol="TAG/USDT",
                timeframe="1h",
                price=1.0,
                action="SELL_FULL",
                exit_source="trailing_stop",
                force_local=True,
            )
        self.assertEqual(r["message"], "no_open_position")
        self.assertFalse(r.get("remote"))


class TestFireHttpAuth(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("EXIT_WS_INTERNAL_TOKEN", None)

    def test_fire_requires_token(self):
        from flask import Flask

        from services.exit_realtime.fire_http import register_exit_ws_fire_routes

        os.environ.pop("EXIT_WS_INTERNAL_TOKEN", None)
        app = Flask(__name__)
        register_exit_ws_fire_routes(app)
        client = app.test_client()
        r = client.post(
            "/internal/exit-ws/fire",
            json={
                "symbol": "TAG/USDT",
                "timeframe": "1h",
                "price": 1.0,
                "exit_source": "trailing_stop",
            },
        )
        self.assertEqual(r.status_code, 503)

    def test_fire_unauthorized(self):
        from flask import Flask

        from services.exit_realtime.fire_http import register_exit_ws_fire_routes

        os.environ["EXIT_WS_INTERNAL_TOKEN"] = "good"
        app = Flask(__name__)
        register_exit_ws_fire_routes(app)
        client = app.test_client()
        r = client.post(
            "/internal/exit-ws/fire",
            json={
                "symbol": "TAG/USDT",
                "timeframe": "1h",
                "price": 1.0,
                "exit_source": "trailing_stop",
            },
            headers={"X-Exit-Ws-Token": "bad"},
        )
        self.assertEqual(r.status_code, 401)

    def test_fire_ok_calls_local_execute(self):
        from flask import Flask

        from services.exit_realtime.fire_http import register_exit_ws_fire_routes

        os.environ["EXIT_WS_INTERNAL_TOKEN"] = "good"
        app = Flask(__name__)
        register_exit_ws_fire_routes(app)
        client = app.test_client()
        with patch(
            "services.exit_realtime.execute.try_execute_trail_exit",
            return_value={
                "ok": True,
                "executed": True,
                "message": "filled",
            },
        ) as mock_ex:
            r = client.post(
                "/internal/exit-ws/fire",
                json={
                    "symbol": "TAG/USDT",
                    "timeframe": "1h",
                    "price": 1.5,
                    "action": "SELL_FULL",
                    "exit_source": "trailing_take_profit",
                    "rationale": "trail",
                },
                headers={"X-Exit-Ws-Token": "good"},
            )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["executed"])
        kwargs = mock_ex.call_args.kwargs
        self.assertTrue(kwargs.get("force_local"))
        self.assertEqual(kwargs["symbol"], "TAG/USDT")


if __name__ == "__main__":
    unittest.main()
