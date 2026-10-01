# Operator sources

Drop for links the lead and the daily loops should know about.
**You send URLs in this chat; the lead appends them here.** Do not edit trading code from this file.

`use`:

- `ideas` — ledger / exchange / recovery / ops (Ideen-Loop, max. eine Inbox-Karte/Tag)
- `research` — strategy / signals / regime (nur Vorschlag, kein Ticket)
- `ops` — Railway, Grok CLI, deploy, login (kein Trading-Pfad)

---

## Standing rotation (Ideen-Loop 08:40 Europe/Berlin)

One source per calendar day, order in `~/.omnigent/ideas-loop-state.json`.

| # | id | URL | use |
|---|---|---|---|
| 0 | gate-docs | https://www.gate.com/docs/developers/apiv4/en/ | ideas |
| 0 | gate-docs changelog | https://www.gate.com/docs/developers/apiv4/en/#changelog | ideas |
| 1 | ccxt-gate | https://github.com/ccxt/ccxt/blob/master/python/ccxt/gate.py | ideas |
| 2 | hummingbot gate | https://github.com/hummingbot/hummingbot | ideas |
| 3 | freqtrade exchange | https://github.com/freqtrade/freqtrade/tree/develop/freqtrade/exchange | ideas |
| 4 | arxiv q-fin.TR | https://arxiv.org/list/q-fin.TR/recent | research (PARKED if alpha/regime) |
| 5 | gh-trading-bot | https://github.com/search?q=trading+bot&type=repositories | ideas (Landscape; max. 1 Repo. Strategie → PARKED) |

Weekly `strategy_improve` (Monday 09:00, tenant henry) reads this file plus the live book. GitHub landscape extra (not in the 08:40 ideas rotation):

| id | URL | note |
|---|---|---|
| gh-trading-strategies | https://github.com/search?q=trading+strategies&type=repositories | Strategie-Repos — abschauen ja, Parameter nicht kopieren |
| nautilus-trader | https://github.com/nautechsystems/nautilus_trader | Event-driven research/backtest/live (Rust core). Prefer on ideas slot 5 over generic search. Docs: https://nautilustrader.io/docs/ |
| ai-hedge-fund | https://github.com/virattt/ai-hedge-fund | Multi-agent PoC — PARKED (signals/agents). Educational, no live trades. |
| finrl | https://github.com/AI4Finance-Foundation/FinRL | RL research — PARKED. Production successor: https://github.com/AI4Finance-Foundation/FinRL-Trading |
| elizaos | https://github.com/elizaOS/eliza | Agent OS + wallets — PARKED. Docs: https://docs.elizaos.ai/ |
| bend | https://github.com/bendlang/bend | Proof language + `LAWS.bend`. PARKED. Steal the *laws* idea (fail-closed invariants as unskippable agent contract), do **not** rewrite the bot in Bend. Site: https://bend-lang.com |
| self-healing-error-router | https://github.com/el23ddg/Self-Healing-Data-Pipeline-with-AI-Error-Diagnosis | n8n + Gemini demo. Steal only: transient / structural / ambiguous + confidence gate + retry cap + GitHub issue vs human. Not n8n, not Gemini, not money-path retry. Ticket #546. |

---

## AI trading stack (Teddy @TeddyinMedia, 2026-09-19)

Source post: https://x.com/teddyinmedia/status/2101311604797145483

Framing in the post: `data → strategy → backtest → risk → execution → agent`. Research starting point, not a money printer. Build locally; no live funds; no wallet access for an agent.

Reply worth keeping (Harley @harleyfoote_): the missing repo is the one that catches when an agent social-engineers its own data sources — fail-closed / source-integrity, not a new strategy.

| # | Repo | URL | Already in rotation? | use | How we treat it |
|---|---|---|---|---|---|
| 1 | AI Hedge Fund | https://github.com/virattt/ai-hedge-fund | no | research | PARKED. Multi-agent debate before a trade. Educational; does not place live orders. Do not copy investor-agent prompts into live. |
| 2 | Freqtrade | https://github.com/freqtrade/freqtrade | yes — standing #3 | ideas | Exchange/retry/order-status only. Ignore `strategies/`. |
| 3 | CCXT | https://github.com/ccxt/ccxt | yes — standing #1 | ideas | We speak Gate through ccxt (`python/ccxt/gate.py`). |
| 4 | NautilusTrader | https://github.com/nautechsystems/nautilus_trader | no (landscape extra) | ideas | Highest signal for us: same strategy code across research and live; fills, recovery, order lifecycle, adapters. Not a strategy-DSL copy. |
| 5 | Hummingbot | https://github.com/hummingbot/hummingbot | yes — standing #2 | ideas | Market-making / order tracker / gate_io adapter. |
| 6 | Eliza | https://github.com/elizaOS/eliza | no | research | PARKED. TypeScript agent OS, wallets, onchain. Not our stack; do not copy wallet automation. |
| 7 | FinRL | https://github.com/AI4Finance-Foundation/FinRL | no | research | PARKED. Classic RL train-test-trade. Next-gen: [FinRL-Trading](https://github.com/AI4Finance-Foundation/FinRL-Trading). Papers: https://arxiv.org/abs/2011.09607 · https://arxiv.org/abs/2603.21330 |
| 8 | Jesse | https://github.com/jesse-ai/jesse | yes — operator drop 2026-09-15 | research | Framework/backtest. Already #249. Do not copy their strategy DSL. |

Related (not in the original 8, linked from FinRL README):

| id | URL | note |
|---|---|---|
| finrl-trading | https://github.com/AI4Finance-Foundation/FinRL-Trading | FinRL-X production stack — still PARKED (RL/live-deploy), not an ideas-loop source. |
| fingpt | https://github.com/AI4Finance-Foundation/FinGPT | Financial LLM research — PARKED. |

---

## Operator drop

Newest first. Status: `open` (not yet consumed) · `queued` (loop will take it) · `done` (Inbox/ticket/skip).

| date | use | status | URL | note |
|---|---|---|---|---|
| 2026-09-22 | ops | queued | https://github.com/kerpopule/hermes-jev-skills | TypeSafe Jev. Installer stays PARKED (`install.py` unused). Operator 2026-09-23 nicked our **local log-only shadow screen** on Hindsight reviewer recalls (`area:infra`). Week-1 review 2026-09-30 in PM-Hub. Reviewer still gets the full list. Not xagent Hermes (`hermes/memory`). Not #508. Handoff summaries measured worse than the full transcript. |
| 2026-09-22 | ops | open | https://github.com/el23ddg/Self-Healing-Data-Pipeline-with-AI-Error-Diagnosis | Meta-workflow: classify fail → retry / GitHub issue / human. Steal the router, not n8n/Gemini. No money-path heal. Ticket #546. |
| 2026-09-21 | research | open | https://github.com/bendlang/bend | Bend 2: `LAWS.bend` = AGENTS.md with compiler proof. PARKED for live. Steal discrete money-path laws (fail-closed, lease, no assertion-weakening); do not port execution/risk. F32 unprovable; no TLS/JSON/Python target yet. |
| 2026-09-20 | ideas | open | https://github.com/nautechsystems/nautilus_trader | NautilusTrader: event-driven research/backtest/live. Abschauen: fills, recovery, order lifecycle, adapters — nicht Strategie-DSL. Docs: https://nautilustrader.io/docs/ |
| 2026-09-20 | research | open | https://github.com/virattt/ai-hedge-fund | Multi-agent hedge-fund PoC (educational, no live trades). PARKED for signals; agent-debate vs our decision-agents. Source: https://x.com/teddyinmedia/status/2101311604797145483 |
| 2026-09-20 | research | open | https://github.com/AI4Finance-Foundation/FinRL | RL research. PARKED. Successor https://github.com/AI4Finance-Foundation/FinRL-Trading . Papers arXiv:2011.09607 / 2603.21330. |
| 2026-09-20 | research | open | https://github.com/elizaOS/eliza | Agent OS + wallets/onchain. PARKED — not our stack; no wallet automation. Docs: https://docs.elizaos.ai/ |
| 2026-09-20 | research | open | https://x.com/teddyinmedia/status/2101311604797145483 | Landscape post (8 repos). Map in **AI trading stack** above. |
| 2026-09-15 | research | open | https://github.com/Drakkar-Software/OctoBot | OctoBot: Multi-Exchange-Bot. Abschauen: Exchange-Adapter, Tentacles/Plugin-Schnitt, Order-Lifecycle — nicht Strategie-Packs 1:1. |
| 2026-09-15 | research | open | https://github.com/stefan-jansen/machine-learning-for-trading | Jansen ML-for-Trading (Buch/Repo). Research: Feature/Label-Hygiene, Walk-forward — nicht Modelle 1:1 in den Live-Bot. PARKED für Signals; IDEA nur wenn es Ledger/Evaluation-Lücken bei uns trifft. |
| 2026-09-15 | research | open | https://github.com/jesse-ai/jesse | Jesse: Framework (Strategie/Backtest/Live). Abschauen: Exchange-Adapter, Order-Lifecycle, Research-Sidecar — nicht deren Strategie-DSL kopieren. Bereits angesprochen in `docs/plan` / #249. |
| 2026-09-15 | ops | open | https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md | Grok CLI `login --device-auth` — headless/Railway. Ticket #397 |
| 2026-09-15 | ops | open | https://docs.railway.com/cloud-agents/configuration | Railway Grok CLI: `~/.grok/auth.json`, sign-in on the instance. #397 |
| 2026-09-15 | ops | open | https://railway.com/agents/grok | SuperGrok / X Premium+ + Railway. #397 |

---

## How to add

In the lead chat, paste:

```
source: <url>
use: ideas | research | ops
note: <one line>
```

The lead appends a row under **Operator drop**.

How loops consume this file:

- **Ideen-Loop 08:40** — standing rotation 0–5. Slot 5 (`gh-trading-bot`) now prefers named landscape extras / `open` ideas-rows here (NautilusTrader first) over the generic GitHub search.
- **strategy_improve Monday 09:00** — reads the whole file. Strategy/RL/agent-framework rows stay PARKED.
