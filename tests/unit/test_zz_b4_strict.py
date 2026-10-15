"""Viktor: PR parallel test with the worker looking up get_bot_config INSIDE the window."""
import threading
from tests.unit.test_shadow_signal_log_687 import T0, _raw, _trigger, logs  # noqa: F401


def test_parallel_thread_late_lookup(logs, monkeypatch):
    import core.config as config_mod
    import strategies.registry as registry_mod
    import strategies.sensor_entry_policy as size_mod

    def _config(tenant_id=None):
        return {"MARKER": "tenant-b" if tenant_id == "tenant-b" else "process", "stop_loss_pct": 7}

    monkeypatch.setattr(config_mod, "get_bot_config", lambda tenant_id=None: config_mod.BotConfig(tenant_id=tenant_id))
    monkeypatch.setattr(config_mod, "get_config", _config)
    monkeypatch.setattr("data_manager.get_config", _config)
    g1 = threading.Barrier(2, timeout=3); g1b = threading.Barrier(2, timeout=3)
    g2 = threading.Barrier(2, timeout=3); g2b = threading.Barrier(2, timeout=3)
    real_r = registry_mod.resolve_strategy_params; real_s = size_mod.resolve_sensor_usdt
    # main thread parks inside the call until the worker has read the config
    monkeypatch.setattr(registry_mod, "resolve_strategy_params", lambda *a, **k: (g1.wait(), g1b.wait(), real_r(*a, **k))[2])
    monkeypatch.setattr(size_mod, "resolve_sensor_usdt", lambda *a, **k: (g2.wait(), g2b.wait(), real_s(*a, **k))[2])
    seen = []

    def worker():
        try:
            for gate, gate_b in ((g1, g1b), (g2, g2b)):
                gate.wait()
                try:
                    seen.append(config_mod.get_bot_config(tenant_id="tenant-b").raw.get("MARKER"))
                    seen.append(registry_mod.get_bot_config(tenant_id="tenant-b").raw.get("MARKER"))
                finally:
                    gate_b.wait()
        except Exception as exc:  # TypeError from a no-arg lambda counts as a fail
            seen.append(repr(exc))

    t = threading.Thread(target=worker); t.start()
    _trigger(logs, _raw(), price=100.0, now=T0)
    t.join(5)
    assert seen == ["tenant-b"] * 4
