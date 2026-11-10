# Short wizard stable symbol (#529)

Status: Rev 1 frozen for this fix, 2026-11-10 Europe/Berlin.
Owner of the issued ticket: Jens Holze. Issue: https://github.com/jholze/xagent-trading-bot/issues/529
Parent epic: #444. Related history, not extra scope: #512, #455, #527, #446.

RECALL_FAILED. Hindsight bank `coding-agent::trading_bot` is not reachable from this cloud VM (Mac loopback only). Git, `AGENTS.md`, and issue #529 are the source for the issued behaviour.

Marcus disposition for this package: the existing ticket acceptance criteria, including the 09-Nov corrections already in the issue, are the basis. Deltas below that are not in the issue are marked **pending Marcus check**. They do not rewrite Rev 0.

This is an existing safety bug in the manual `/short` wizard. It is not a strategy activation, not a new order route, and not behind a new feature flag. `shorts.allow_live` stays false. Manual confirmation and `manual_ok` stay mandatory.

## Rev 0 — issued 2026-11-08, unchanged

Copied from issue #529. Not rewritten.

- AC-529-R0-1. Size tap after a coin tap still confirms the coin that was tapped, even if the watchlist re-sorted.
- AC-529-R0-2. Empty-watchlist `/short` still accepts a typed ticker next.
- AC-529-R0-3. Frozen `manual_order_flow` short/cover tests stay green except assertions this ticket names.
- AC-529-R0-4. Tests write nothing into `data/`.
- AC-529-R0-5. Opus required (order path).

Proposed fix in the issue, still the mechanism: carry the ticker (not the display index) from the coin tap into the size tap, the same identity shape as `encode_lot_callback` on cover. Arm command context on the empty or unavailable watchlist path so a typed ticker still resolves. Log swallowed `list_coins` / `get_prices_batch` failures, fail closed, and do not log secrets.

## Rev 1 — 2026-11-10 Europe/Berlin

Rev 0 stays in force. Rev 1 names the observable contract this change freezes, including the callback payload the issue's proposed fix requires.

Out of scope: `execution/`, `risk/`, `portfolio`, `strategies/short_policy.py`, `strategies/short_cover.py`, config, live or testnet orders, Telegram sends to a real bot, cover-lot callbacks, `/buy` display indexes.

### AC1 — stable identity after the coin tap

Given a rendered `/short` coin button for symbol S, a later size tap confirms S and no other symbol when the watchlist is reordered, rotated, or has had S removed between the two taps. The size step must not call `resolve_coin_by_display_index`. The value stored for the size step is the ticker, never the display index.

Acceptance test: `tests/unit/test_short_wizard_stable_symbol_529.py::TestShortWizardStableSymbol529::test_ac1_size_tap_confirms_tapped_symbol_after_reorder_rotate_remove`.

### AC2 — callback carries the ticker

The coin button `callback_data` is `shortcoin:<TICKER>` (Telegram's 64-byte cap), not `shortcoin:<display index>`. A numeric or otherwise non-ticker payload is stale: explicit failure, no size step, no confirmation, no other coin.

Named frozen-assertion updates in `tests/unit/test_short_cover_wizards_512.py`, because they pinned the unsafe index payload:

- `test_bare_short_taps_watchlist_and_does_not_confirm` expected callbacks `shortcoin:1`, `shortcoin:2` become `shortcoin:ARIA`, `shortcoin:SOL` for that fixture.
- `test_short_back_returns_to_coin_picker` expected leading callbacks change the same way.
- Happy-path, size-preset, and back tests tap `shortcoin:ARIA` where they previously tapped `shortcoin:1` (index 1 was `ARIA/USDT` in that fixture).

No other assertion in that file, and no assertion in `test_short_cover_confirm.py`, `test_short_telegram_ux.py`, or `test_manual_order_flow.py`, is an intended contract change.

Acceptance tests: `test_ac2_picker_callback_is_ticker_not_index` and `test_ac2_numeric_callback_does_not_select_a_different_coin`.

### AC3 — legacy index stored in the size step fails closed

A size step whose stored coin is a display index (the pre-fix context shape) does not resolve that index against the current watchlist and does not confirm whichever coin now sits at that index.

Acceptance test: `test_ac3_legacy_index_in_size_context_does_not_reresolve`.

### AC4 — empty or unavailable watchlist still takes a typed ticker

Bare `/short` with an empty watchlist, or with `list_coins` raising, still arms `short` / `short_awaiting_coin` for the current chat. The next typed ticker confirms that ticker through the existing confirmation request. It is not dropped.

Acceptance tests: `test_ac4_empty_watchlist_arms_typed_ticker`, `test_ac4_list_coins_failure_arms_typed_ticker_and_logs_without_secrets`.

### AC5 — swallowed list and price failures are logged without secrets

`list_coins` and `get_prices_batch` exceptions are logged at WARNING with the step name. The log line must not contain secret-shaped values or URLs from the exception text. A price failure still shows ticker buttons and does not confirm an order. A list failure takes the AC4 path.

Acceptance tests: `test_ac4_list_coins_failure_arms_typed_ticker_and_logs_without_secrets`, `test_ac5_price_failure_logs_without_secrets_and_keeps_ticker_buttons`.

### AC6 — stale, wrong-chat, and wrong-tenant callbacks do not arm an order

A callback with no chat id, a chat that does not own the armed wizard, a tenant or scope other than the one stored when the wizard was armed, or a size callback whose state is not `short_awaiting_usdt`, does not call `request_short_confirmation` and does not replace another chat's wizard.

Tenant and scope binding is a delta beyond the issued ticket text. **Pending Marcus check.** The check is fail-closed and does not add an order route. Rows armed before this revision have no stored tenant; a numeric coin is still rejected by AC3. New arming stores `tenant_id`, `scope`, and `bound_chat_id`.

Acceptance tests: `test_ac6_wrong_chat_does_not_confirm`, `test_ac6_wrong_tenant_does_not_confirm`, `test_ac6_stale_size_callback_does_not_confirm`, plus the existing `test_shortcoin_missing_chat_id_fails_closed` in `test_short_cover_wizards_512.py`.

### AC7 — confirmation gate unchanged

The coin tap does not confirm. The size tap calls `request_short_confirmation` for the stored symbol and does not call `execute_short`. One tap is not an order. `manual_ok` remains the only execute path (unchanged module `manual_order_flow`).

A selected symbol with no price is the existing explicit "Kein Preis für `<symbol>`" failure for that same symbol, not a substitute coin.

Acceptance tests: `test_ac7_size_tap_previews_and_does_not_execute`, `test_ac7_missing_price_names_the_selected_symbol_only`.

### AC8 — no checkout data writes

The new tests and the touched wizard tests redirect command context off `data/`. Running them must not create or modify files under the checkout `data/` directory.

Checked by the data-write audit recorded in `docs/evidence/529.md`.

## AC to test map

| AC | Test |
| --- | --- |
| AC1 | `test_ac1_size_tap_confirms_tapped_symbol_after_reorder_rotate_remove` |
| AC2 | `test_ac2_picker_callback_is_ticker_not_index`, `test_ac2_numeric_callback_does_not_select_a_different_coin`, named edits in `test_short_cover_wizards_512.py` |
| AC3 | `test_ac3_legacy_index_in_size_context_does_not_reresolve` |
| AC4 | `test_ac4_empty_watchlist_arms_typed_ticker`, `test_ac4_list_coins_failure_arms_typed_ticker_and_logs_without_secrets` |
| AC5 | `test_ac5_price_failure_logs_without_secrets_and_keeps_ticker_buttons`, `test_ac4_list_coins_failure_arms_typed_ticker_and_logs_without_secrets` |
| AC6 | `test_ac6_wrong_chat_does_not_confirm`, `test_ac6_wrong_tenant_does_not_confirm`, `test_ac6_stale_size_callback_does_not_confirm`, existing missing-chat test |
| AC7 | `test_ac7_size_tap_previews_and_does_not_execute`, `test_ac7_missing_price_names_the_selected_symbol_only`, existing `test_short_cover_confirm.py` |
| AC8 | data-write check in `docs/evidence/529.md` |
| Rev 0 Opus | not run by the coder; parent arranges `claude-opus-5-5` on the final head |

## Evidence metric

Before: a size step that still holds display index `1` confirms whichever coin is now first. After: that step confirms nothing, and a ticker button still confirms the tapped symbol after the watchlist changes. Count is the new test module plus the existing short wizard/confirm modules. No profitability claim.
