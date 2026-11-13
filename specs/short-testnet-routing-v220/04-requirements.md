# Gate futures testnet routing – Requirements

Version: 2.0 · Date: 2026-11-10 · Status: draft · Owner: Jens Holze

Local draft id: T220-01 (not a GitHub issue). Existing in memory: RECALL_FAILED
(Hindsight is only reachable via the Mac loopback; recorded by the 10 Nov 2026
commission). Pending Marcus check: every AC below that is not already the
closed #347 / #380 contract. Kay coordinates that check. This revision does
not rewrite #347.

## Rev 1 archive

Rev 1 full text was not in this worker's dispatch attachments
(`/workspace/umbau/v220_planning_2026-11-09/drafts/T220-01.md` and
`T220-01_host_allowlist_DRAFT.md`). This file does not invent that text.
Unchanged safety rules carried forward from the 10 Nov sprint acceptance and
from #347: testnet only, no mainnet fallback, no live credentials, one named
tenant, `shorts.allow_live` stays false, no real account-mode switch.

## Why

#347 already points testnet SHORT/COVER at `https://api-testnet.gateapi.io`
behind `shorts.testnet_futures` (default off, empty tenant list). The route is
still only as safe as "ccxt sandbox mode was set". T220-01 asks for an
explicit origin check before signing, a mode proof that does not trust the set
response, and a contract conversion that cannot round above its bounds.

Package A still has exclusive edits on `execution/gate_adapter.py` and
`execution/recovery.py` while #772, #800, and #801 are open. This revision
therefore adds `execution/futures_testnet_route.py` and does not wire it.

## Frozen origin

Checked 2026-11-10, no order sent:

| Source | Futures testnet REST base |
| --- | --- |
| Gate API v4.106.180 "TestNet trading" | `https://api-testnet.gateapi.io/api/v4` |
| ccxt 4.5.88 `gate.urls['test']['private']['futures']` | `https://api-testnet.gateapi.io/api/v4` |
| Live, not a fallback | `https://api.gateio.ws/api/v4`, `https://fx-api.gateio.ws/api/v4` |
| Legacy v1 changelog host, not a fallback | `fx-api-testnet.gateio.ws` |

Normalized futures-USDT paths are `/api/v4/futures/usdt` and its slash-separated
segments. The legacy host is rejected with `legacy_host_rejected` and
`hosts_after_failure` is empty.

## Flag

`shorts.testnet_futures.strict_route` must be literal `true`. Missing, false,
1, and the string `"true"` are off. Off returns `strict_route_off`,
`inhibits_existing_adapter=false`, and zero transport calls. `shorts.allow_live`
and `shorts.climax_fade.enabled` do not turn the guard on. Committed
`config.json` is not modified; the shipped `testnet_futures.enabled` value
stays false.

## AC1 Origin before signing

Given the flag, testnet mode, and the one named tenant, a POST is allowed only
when the URL is https, host `api-testnet.gateapi.io`, port empty or 443, and
the normalized path is the futures-USDT prefix. HTTP, a non-443 port, a host
suffix, userinfo, `..`, encoded dot-segments, encoded slashes, mainnet hosts,
and the legacy host are rejected with zero transport calls. A 3xx response is
not followed, including a cross-origin Location. No decision stores userinfo.

Test: `tests/unit/test_v220_t220_01.py::test_ac_t01_pinned_origin_accepted_before_transport`
Test: `test_ac_t01_rejects_bad_origin_without_transport`
Test: `test_ac_t01_userinfo_secret_is_not_in_the_decision`
Test: `test_ac_t01_cross_origin_redirect_is_not_followed`

## AC2 Flag, mode, one tenant, redaction

Empty tenant context denies. The tenant list must be exactly one non-empty
string equal to the current tenant. Live, real, and shadow modes deny.
Two evaluations do not share tenant state. Missing testnet credential env
denies and does not select the live env; the function does not read process
secrets. Redaction removes api_key, apiKey, secret, passphrase, sign,
signature, authorization, KEY, and SIGN.

Test: `test_ac_t01_missing_flag_and_legacy_activation_do_not_enable`
Test: `test_ac_t01_tenant_mode_matrix`
Test: `test_ac_t01_tenants_do_not_share_decisions`
Test: `test_ac_t01_missing_testnet_credentials_do_not_use_live_env`
Test: `test_ac_t01_redaction_matrix`

Pending Marcus check: "exactly one tenant" is stricter than the shipped #347
membership test, which allows a longer list. The stricter rule is only inside
this default-off guard.

## AC3 Contract size

Conversion uses Decimal. Step rounding is toward zero. The result must be
<= the exact quotient and <= maximum, and >= minimum. Exact-at-limit passes.
A stepped quantity above maximum is `above_max` with `contracts is None`, not
a clamped success and not zero. Unknown, non-finite, or non-positive size,
minimum, step, or maximum is an unknown/deny, not a zero order.

Test: `test_ac_t01_contract_bounds_do_not_round_up`
Test: `test_ac_t01_unknown_contract_inputs_deny`
Test: `test_ac_t01_decimal_string_does_not_use_binary_float_spill`

## AC4 Mode proof

Isolated one-way requires both a set record and a readback. They must agree.
Readback wins when they disagree: cross, leverage 0 (even if the label says
isolated), dual, hedge, dual_plus, unknown margin, unknown position, unknown
leverage, missing timestamp, and age greater than the supplied max all deny.
`collect_mode_proof` calls set only when `allow_set` is true and
`client.synthetic` is true. A non-synthetic client is never switched.

Test: `test_ac_t01_bad_mode_proof_denies`
Test: `test_ac_t01_set_response_without_readback_denies`
Test: `test_ac_t01_readback_without_set_denies`
Test: `test_ac_t01_mismatched_readback_denies_even_if_set_is_isolated`
Test: `test_ac_t01_stale_and_missing_timestamps_deny`
Test: `test_ac_t01_real_client_is_not_switched`
Test: `test_ac_t01_synthetic_set_still_needs_matching_readback`
Test: `test_ac_t01_synthetic_set_and_readback_allow`

Pending Marcus check: requiring a set record even when readback is already
isolated one-way, and treating a missing leverage field as unknown rather than
inferring isolated from a non-zero leverage (the inference in
`execution/recovery.py` `_margin_mode_of` is unchanged).

## Non-goals

No edit to `execution/gate_adapter.py`, `execution/recovery.py`, `config.json`,
strategy thresholds, or the shared `specs/README.md` index. No exchange call,
no keys, no emergency flatten (T220-02). #412 stays closed NOT_PLANNED.

## Changelog

| Version | Date | AC | Change | Reason | Approved by |
| --- | --- | --- | --- | --- | --- |
| Rev 1 | 2026-11-09 | prior pack | Not attached to this worker. Not rewritten here. | Local planning pack | Marcus 09 Nov corrections are not approval of these deltas |
| Rev 2 | 2026-11-10 | AC1–AC4 | Default-off route guard, origin pin, tests. Not wired. | 10 Nov sprint package B | Pending Marcus check on the deltas named above |
