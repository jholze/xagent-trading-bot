# D-LIN handoff for G and H

Version: 1.1 · Date: 2026-11-10 · Status: contract for downstream packages · Owner: Kay

One page. Observational only. Not an activation, not a testnet pass, not a PnL rule.

## What F stores

- Lot `entry_snapshot`: unchanged #600 keys. First fill only. Later DCA/partial/replay must not replace it.
- Lot `entry_lineage`: schema `d-lin-1`. Decision values, `missing` reasons, optional `extended` whitelist, `entry_order_id`, `entry_allocation_id`.
- Order row `entry_lineage`: the decision block written before the fill.
- Order row `exit_lineage.allocations[]`: `{entry_allocation_id, entry_order_id, entry_source, exit_order_id, qty}`.
- Rejected attempt: `attempt_id`, `attempt_only: true`. MCP `is_fill` is false for that row.

`entry_allocation_id` is `ord:<order id>` when the fill had an order id, otherwise `alloc:<random hex>` stored once. It is not a symbol plus timestamp.

## G (accounting / replay)

- Join an exit to its entry with `exit_lineage.allocations[].entry_allocation_id` and `entry_order_id`.
- Do not match sells to buys by symbol and clock time.
- A missing id is unlinked. Do not invent one and do not treat it as zero PnL.
- This block is not a fee, funding, or cash-flow figure. Keep G's own provenance.
- Replaying a lot must not overwrite `entry_snapshot` or `entry_lineage` on an open lot. Both are on the position cache field list so a rebuild can keep them. A new cycle pops them (same reset as the snapshot).

## H (offline harness)

- Read lots/orders through the existing MCP projection. It returns the stored snapshot and the public lineage. It does not recompute RSI from the current book.
- Fixture coverage, including a 100% supplied-fixture result, is not an authenticated testnet pass. `lineage_coverage()["runtime_target_measured"]` is false. Do not report the 90% operational target as measured.
- Rejected attempts stay non-fills. Do not count them as entries.
- No new global kill switch. `shorts.allow_live` stays false. No exchange call is added here.

## Adapter note for B

`execution/gate_adapter.py` is not edited in this package. `_sync_local_ledger` already passes `order_id`. Portfolio loads `entry_lineage` from that order. A future fill path that books a position without an order row must pass `lineage=` into `PortfolioService.execute_buy` / `execute_short`. It must not fetch indicators at fill time.
