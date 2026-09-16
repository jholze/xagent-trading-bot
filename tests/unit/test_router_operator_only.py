"""#451: router denies operator-only commands for non-operator chats.

Hiding a command from the satellite menu (#398) is not a gate — the typed
path (`/live_confirm`, `/reload all`, `/sandbox_promote`, …) reached every
handler. ``OPERATOR_ONLY`` in the router is checked for typed commands AND
for inline callbacks, so both paths share one allow/deny decision.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from notifications.telegram_commands import router
from notifications.telegram_commands.command_context import _chat_id_var
from notifications.telegram_commands.menu_commands import (
    MENU_SECTIONS_OPERATOR,
    MENU_SECTIONS_SATELLITE,
)
from tests.support.telegram_capture import install_telegram_capture, texts

OPERATOR_CHAT = "12345"  # conftest telegram_credentials
SATELLITE_CHAT = "999"

DENY_TEXT = "Nur Operator"


@pytest.fixture(autouse=True)
def _clean_chat_context():
    """Start every test with no chat context.

    ``_chat_id_var`` is process-global; a test elsewhere that runs the real
    ``menu_commands.handle_callback`` (e.g. ``test_satellite_callback_runs_panic``)
    calls ``set_chat_id`` and never resets it. Without this reset, a leaked
    satellite/operator chat id would leak into ``test_no_chat_context_is_operator``
    and friends. ``as_chat`` layers on top of this baseline.
    """
    tok = _chat_id_var.set("")
    yield
    _chat_id_var.reset(tok)


@pytest.fixture
def tg(monkeypatch):
    return install_telegram_capture(monkeypatch)


@pytest.fixture
def multi_tenant():
    """Multi-tenant on; any non-operator chat resolves to tenant ``henry``."""
    with patch("core.tenant_context.multi_tenant_enabled", return_value=True), patch(
        "storage.tenant_registry.find_tenant_by_owner_chat_id",
        return_value={"tenant_id": "henry"},
    ):
        yield


@pytest.fixture
def as_chat():
    tokens = []

    def _set(chat_id: str):
        tokens.append(_chat_id_var.set(str(chat_id)))

    yield _set
    for tok in reversed(tokens):
        _chat_id_var.reset(tok)


@pytest.fixture
def handler_spy(monkeypatch):
    """Replace the handler chain with a recorder; returns the call list."""
    calls: list[str] = []

    def _spy(text: str) -> bool:
        calls.append(text)
        return True

    monkeypatch.setattr(router, "_HANDLERS", [_spy])
    return calls


def _menu_cb(key: str, chat_id=SATELLITE_CHAT) -> dict:
    return {"id": "cq", "data": f"menu:run:{key}", "message": {"chat": {"id": int(chat_id)}, "message_id": 7}}


# --------------------------------------------------------------------------
# Typed path
# --------------------------------------------------------------------------


class TestTypedPathSatellite:
    @pytest.mark.parametrize(
        "cmd",
        [
            "/live_confirm",
            "/live_cancel",
            "/reload all",
            "/reload",
            "/hotreload",
            "/sandbox_promote",
            "/sandbox",
            "/backtest BTC/USDT",
            "/hermes_run",
            "/hermes_veto exp1",
            "/hermes_rollback",
            "/diag",
            "/testaccount @foo 3",
            "/tracktest",
            "/wqe",
            "/churn_replay",
            "/counterfactual",
            "/session_cancel",
            "/panic",
            "/help onboarding",
            "/LIVE_CONFIRM",
            "/live_confirm@XAgentBot",
        ],
    )
    def test_denied_not_executed(self, tg, multi_tenant, as_chat, handler_spy, cmd):
        as_chat(SATELLITE_CHAT)
        assert router.dispatch_command(cmd) is True
        assert handler_spy == [], f"{cmd} reached a handler for a satellite chat"
        assert len(tg) == 1
        assert DENY_TEXT in tg[0]["text"]
        # do not teach the command
        assert cmd.split()[0].lstrip("/").split("@")[0].lower() not in tg[0]["text"].lower()

    @pytest.mark.parametrize("cmd", ["/positions", "/mode", "/help", "/pause", "/resume", "/buy BTC/USDT 10"])
    def test_allowed_reaches_handler(self, tg, multi_tenant, as_chat, handler_spy, cmd):
        as_chat(SATELLITE_CHAT)
        assert router.dispatch_command(cmd) is True
        assert handler_spy == [cmd]
        assert not any(DENY_TEXT in t for t in texts(tg))

    def test_help_without_subcommand_allowed(self, tg, multi_tenant, as_chat, handler_spy):
        as_chat(SATELLITE_CHAT)
        router.dispatch_command("/help")
        assert handler_spy == ["/help"]

    def test_unknown_chat_fails_closed(self, tg, as_chat, handler_spy):
        """Registry lookup fails → still satellite → still denied."""
        as_chat("31337")
        with patch("core.tenant_context.multi_tenant_enabled", return_value=True), patch(
            "storage.tenant_registry.find_tenant_by_owner_chat_id",
            side_effect=RuntimeError("mongo down"),
        ):
            assert router.dispatch_command("/live_confirm") is True
        assert handler_spy == []
        assert DENY_TEXT in tg[0]["text"]


class TestTypedPathOperator:
    @pytest.mark.parametrize("cmd", ["/live_confirm", "/reload all", "/sandbox_promote", "/panic", "/help onboarding"])
    def test_operator_chat_unchanged(self, tg, multi_tenant, as_chat, handler_spy, cmd):
        as_chat(OPERATOR_CHAT)
        assert router.dispatch_command(cmd) is True
        assert handler_spy == [cmd]
        assert not any(DENY_TEXT in t for t in texts(tg))

    def test_single_tenant_is_operator(self, tg, as_chat, handler_spy):
        """MULTI_TENANT_ENABLED=0 (conftest default): no satellite role exists."""
        as_chat("777")
        with patch("core.tenant_context.multi_tenant_enabled", return_value=False):
            assert router.dispatch_command("/reload all") is True
        assert handler_spy == ["/reload all"]

    def test_no_chat_context_is_operator(self, tg, multi_tenant, handler_spy):
        """Internal dispatch (no chat set) falls back to TELEGRAM_CHAT_ID → operator."""
        assert router.dispatch_command("/diag") is True
        assert handler_spy == ["/diag"]


# --------------------------------------------------------------------------
# Callback path
# --------------------------------------------------------------------------


class TestCallbackPath:
    def test_menu_run_denied_for_satellite(self, tg, multi_tenant):
        with patch.object(router.menu_commands, "handle_callback") as menu_cb, patch.object(
            router, "answer_callback_query"
        ) as ans:
            assert router.dispatch_callback(_menu_cb("live_confirm")) is True
        menu_cb.assert_not_called()
        ans.assert_called_once()
        assert DENY_TEXT in ans.call_args.args[1]
        assert tg == []

    def test_menu_run_allowed_for_operator(self, tg, multi_tenant):
        with patch.object(router.menu_commands, "handle_callback", return_value=True) as menu_cb, patch.object(
            router, "answer_callback_query"
        ) as ans:
            assert router.dispatch_callback(_menu_cb("live_confirm", chat_id=OPERATOR_CHAT)) is True
        menu_cb.assert_called_once()
        ans.assert_not_called()

    def test_menu_run_non_gated_passes_for_satellite(self, tg, multi_tenant):
        with patch.object(router.menu_commands, "handle_callback", return_value=True) as menu_cb:
            assert router.dispatch_callback(_menu_cb("positions")) is True
        menu_cb.assert_called_once()

    def test_panic_callback_denied_for_satellite(self, tg, multi_tenant):
        cb = {"id": "cq", "data": "panic_ok:tok", "message": {"chat": {"id": int(SATELLITE_CHAT)}}}
        with patch.object(router.pause_commands, "handle_callback") as pause_cb, patch.object(
            router, "answer_callback_query"
        ):
            assert router.dispatch_callback(cb) is True
        pause_cb.assert_not_called()

    def test_testaccount_callback_denied_for_satellite(self, tg, multi_tenant):
        cb = {"id": "cq", "data": "testaccount_add:foo", "message": {"chat": {"id": int(SATELLITE_CHAT)}}}
        with patch.object(router.x_commands, "handle_callback") as x_cb, patch.object(
            router, "answer_callback_query"
        ):
            assert router.dispatch_callback(cb) is True
        x_cb.assert_not_called()

    def test_gated_callback_without_chat_fails_closed(self, tg, multi_tenant):
        cb = {"id": "cq", "data": "panic_ok:tok"}
        with patch.object(router.pause_commands, "handle_callback") as pause_cb, patch.object(
            router, "answer_callback_query"
        ) as ans:
            assert router.dispatch_callback(cb) is True
        pause_cb.assert_not_called()
        ans.assert_called_once()

    def test_satellite_menu_run_panic_real_path_does_not_execute(self, tg, multi_tenant):
        """Production path: the webhook calls ``router.dispatch_callback`` for
        ``menu:run:panic`` from a satellite chat. ``menu_commands.handle_callback``
        is NOT patched — the router must deny before the menu ever dispatches
        ``/panic``, and answer the deny toast (not the menu's command echo)."""
        with patch.object(router, "answer_callback_query") as router_ans, patch.object(
            router.menu_commands, "answer_callback_query"
        ) as menu_ans, patch.object(router, "dispatch_command") as dispatch, patch.object(
            router.pause_commands, "_preview_panic"
        ) as preview, patch.object(router.pause_commands, "collect_panic_lots") as lots:
            assert router.dispatch_callback(_menu_cb("panic")) is True
        dispatch.assert_not_called()
        preview.assert_not_called()
        lots.assert_not_called()
        menu_ans.assert_not_called()
        router_ans.assert_called_once()
        assert router_ans.call_args.args[0] == "cq"
        assert DENY_TEXT in router_ans.call_args.args[1]
        assert tg == []

    def test_operator_menu_run_panic_real_path_dispatches(self, tg, multi_tenant):
        """Same real path for the operator chat: the router lets the menu run
        ``/panic`` (menu echoes the command as its toast, no deny)."""
        with patch.object(router, "answer_callback_query") as router_ans, patch.object(
            router.menu_commands, "answer_callback_query"
        ) as menu_ans, patch.object(router, "dispatch_command", return_value=True) as dispatch:
            assert router.dispatch_callback(_menu_cb("panic", chat_id=OPERATOR_CHAT)) is True
        dispatch.assert_called_once_with("/panic")
        router_ans.assert_not_called()
        menu_ans.assert_called_once_with("cq", "/panic")

    def test_manual_order_callback_not_gated(self, tg, multi_tenant):
        cb = {"id": "cq", "data": "manual_ok:abc", "message": {"chat": {"id": int(SATELLITE_CHAT)}}}
        with patch.object(router.trading_commands, "handle_callback", return_value=True) as tr_cb:
            assert router.dispatch_callback(cb) is True
        tr_cb.assert_called_once()


# --------------------------------------------------------------------------
# One set, both paths, trimmed against reality
# --------------------------------------------------------------------------


class TestOperatorOnlySet:
    def test_typed_and_menu_run_share_decision(self):
        for key in (k for _, keys in MENU_SECTIONS_OPERATOR for k in keys):
            from notifications.telegram_commands.menu_commands import command_dispatch_text

            typed = router._is_operator_only_text(command_dispatch_text(key))
            inline = router._is_operator_only_callback(f"menu:run:{key}")
            assert typed == inline, f"menu key {key!r}: typed={typed} inline={inline}"

    def test_operator_menu_gap_is_covered(self):
        """Every key the satellite menu hides is gated (onboard self-gates)."""
        sat = {k for _, keys in MENU_SECTIONS_SATELLITE for k in keys}
        hidden = {k for _, keys in MENU_SECTIONS_OPERATOR for k in keys} - sat - {"onboard"}
        assert hidden, "sanity: operator menu must hide something from satellites"
        missing = {k for k in hidden if k not in router.OPERATOR_ONLY}
        assert missing == set(), f"hidden but not gated: {sorted(missing)}"

    def test_satellite_menu_keys_not_gated_except_named(self):
        """Ticket #451 names panic (typed hole) and reload as gated despite being
        on the satellite menu; everything else the satellite sees must run."""
        sat = {k for _, keys in MENU_SECTIONS_SATELLITE for k in keys}
        gated_on_sat = sat & router.OPERATOR_ONLY
        assert gated_on_sat == {"panic", "reload"}

    def test_every_entry_is_a_real_typed_command(self):
        """Trim, do not guess: each entry must be a literal ``"/<cmd>"`` some
        handler module matches on."""
        pkg = Path(router.__file__).parent
        literals: set[str] = set()
        for src in pkg.glob("*.py"):
            if src.name == "router.py":
                continue
            literals.update(
                m.lower() for m in re.findall(r'"/([a-z_?üä]+)', src.read_text(encoding="utf-8"), re.I)
            )
        unknown = {e for e in router.OPERATOR_ONLY if e.split()[0] not in literals}
        assert unknown == set(), f"not a typed command in any handler: {sorted(unknown)}"

    def test_two_word_entries_only_gate_that_subcommand(self):
        assert router._is_operator_only_text("/help onboarding")
        assert router._is_operator_only_text("/help ONBOARD")
        assert not router._is_operator_only_text("/help")
        assert not router._is_operator_only_text("/help trading")
        assert not router._is_operator_only_text("hello world")

    def test_self_gating_handlers_stay_out(self):
        assert "onboard" not in router.OPERATOR_ONLY
        assert "xai_login" not in router.OPERATOR_ONLY

    def test_locale_has_both_languages(self):
        from notifications.telegram_i18n import t

        assert t("command_operator_only", lang="de") == "⛔ Nur Operator."
        assert t("command_operator_only", lang="en") == "⛔ Operator only."

    def test_config_and_config_revert_are_gated(self):
        assert "config" in router.OPERATOR_ONLY
        assert "config revert" in router.OPERATOR_ONLY
        assert router._is_operator_only_text("/config")
        assert router._is_operator_only_text("/config diff")
        assert router._is_operator_only_text("/config revert 1")
        assert router._is_operator_only_callback("config_ok:tok")
        assert router._is_operator_only_callback("config_no:tok")


class TestConfigOperatorOnly:
    """#330 slice 2: /config is operator-only on typed and callback paths."""

    def test_typed_config_denied_for_satellite(self, tg, multi_tenant, as_chat, handler_spy):
        as_chat(SATELLITE_CHAT)
        for cmd in ("/config", "/config diff", "/config diff 1", "/config revert 1"):
            handler_spy.clear()
            tg.clear()
            assert router.dispatch_command(cmd) is True
            assert handler_spy == [], f"{cmd} reached a handler for a satellite chat"
            assert DENY_TEXT in tg[0]["text"]

    def test_typed_config_allowed_for_operator(self, tg, multi_tenant, as_chat, handler_spy):
        as_chat(OPERATOR_CHAT)
        assert router.dispatch_command("/config") is True
        assert handler_spy == ["/config"]

    def test_config_callback_denied_for_satellite(self, tg, multi_tenant):
        cb = {"id": "cq", "data": "config_ok:tok", "message": {"chat": {"id": int(SATELLITE_CHAT)}}}
        with patch.object(router.config_commands, "handle_callback") as cfg_cb, patch.object(
            router, "answer_callback_query"
        ):
            assert router.dispatch_callback(cb) is True
        cfg_cb.assert_not_called()
