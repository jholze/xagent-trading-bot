# Epic #107 — CMC MCP / Agent Hub (sketch)

> **Status:** Sketch only · no implementation yet  
> **Issue:** [#107](https://github.com/jholze/xagent-trading-bot/issues/107)  
> **Split from:** #103 D8 (MCP was priority-1 in the original access strategy)  
> **Sibling (live today):** #105 CMC Pro REST → `coin_facts_cmc_pro.py`

---

## 1. One-liner

**Talk to CoinMarketCap through the official MCP (tool protocol), turn tool results into the same coin-fact events we already use for Memory/RAG/DCA — without scraping HTML and without replacing REST Pro until MCP is proven.**

---

## 2. What MCP is (vs what we have)

| | **Today (staging)** | **#107 MCP** |
|--|---------------------|--------------|
| Transport | REST `pro-api.coinmarketcap.com` | MCP `mcp.coinmarketcap.com/mcp` |
| Consumer | Hermes `sync_coin_facts` (bot loop) | Hermes bridge **and/or** Cursor/Claude agents |
| Auth | `CMC_API_KEY` header | Same key family / Agent Hub (to confirm in M1) |
| Shape | JSON quotes → heuristics | Tool calls → structured skill payloads |
| Status | **Live** (#105) | **Not in repo** |

Product: [coinmarketcap.com/api/mcp](https://coinmarketcap.com/api/mcp/)  
Docs hub: [AI Agent Hub](https://pro.coinmarketcap.com/api/documentation/ai-agent-hub)

MCP is **not** a secret REST for `cmc-ai/*` pages. It is a **tool interface** for agents. We use it as a **provider** into our existing bus.

---

## 3. Target architecture

```text
                    ┌─────────────────────┐
                    │  CMC MCP Server     │
                    │  (tools / skills)   │
                    └──────────┬──────────┘
                               │ MCP (JSON-RPC / SSE — as per CMC docs)
                               ▼
┌──────────────────────────────────────────────────────────────┐
│  Hermes (or small bridge process)                            │
│  cmc_mcp_client  →  list_tools / call_tool                   │
│         │                                                    │
│         ▼                                                    │
│  map_tool_result → CoinFactDraft[]  (same taxonomy as D8)    │
│         │                                                    │
│         ▼                                                    │
│  persist → memory_market_events  → RAG index (existing)      │
└──────────────────────────────────────────────────────────────┘
                               │
                               ▼
              build_dca_context / fact_* policy  (already shipped)
```

**Reuse, do not rebuild:**

- `CoinFactDraft`, `EVENT_TYPES`, `flags_from_events`, `apply_coin_fact_policy`
- `sync_coin_facts` / Hermes cycle (add source `cmc_mcp` next to `cmc_pro` / `cmc_ai`)
- Kill-switch + fail-open pattern from #105

**Parallel providers (config):**

```json
"memory": {
  "coin_facts": {
    "enabled": true,
    "sources": {
      "cmc_pro": { "enabled": true },
      "cmc_mcp": { "enabled": false, "url": "https://mcp.coinmarketcap.com/mcp", "timeout_sec": 30 },
      "cmc_ai":  { "enabled": false }
    }
  }
}
```

Default: **MCP off** until M4 soak; REST Pro stays production path.

---

## 4. Why bother (value)

| Gain | Note |
|------|------|
| ToS-friendlier than HTML scrape | Official agent surface |
| Possibly richer “analysis / narrative” tools | Depends on plan / skill catalog (M1 must prove) |
| Same key, second transport | Ops already have `CMC_API_KEY` on Hermes |
| Optional IDE use | Cursor can use CMC MCP for research; bot uses bridge for memory |

| Non-goal | |
|----------|--|
| Replace #105 on day one | REST remains default until MCP ≥ REST reliability |
| Grok hot-path DCA | Facts only; policy stays pure |
| Full catalog crawl | Universe = open + watchlist, capped |

---

## 5. Children (implement order)

| ID | Title | Deliverable | Depends |
|----|--------|-------------|---------|
| **M1** | Tools inventory + mapping | Doc: tools/skills on **our** plan; map → `event_type` / skip list; credit estimate | — |
| **M2** | MCP client / Hermes bridge | Auth, health, `list_tools`, `call_tool`, timeouts, retries; unit tests with mock transport | M1 |
| **M3** | Ingest adapter | `collect_cmc_mcp_drafts` → `sync_coin_facts`; `source=cmc_mcp_*`; idempotent ids | M2 |
| **M4** | Staging soak | Enable on Hermes test; compare event density vs Pro; no ledger damage | M3 |
| **M5** | Operator docs (optional) | Cursor MCP snippet; when to use IDE vs bot loop | M2 |

**Create GitHub issues for M1–M5 only when starting work** (avoid ticket spam).

---

## 6. M1 discovery checklist (first real work)

1. With production-like `CMC_API_KEY`, connect MCP and list tools.  
2. For each tool: inputs, sample output, rate/credit notes.  
3. Map to taxonomy (same as D8):

   | Fact need | Prefer tool if available | Fallback |
   |-----------|--------------------------|----------|
   | 24h move / volume | market / quotes skill | REST Pro (#105) |
   | Narrative / news | news / content skill | RSS / HTML |
   | Structure / levels | analysis skill if any | HTML cmc-ai |
   | Unlocks / supply | only if tool is explicit | do not invent from price |

4. Decision gate after M1:

   - **Go** if ≥1 tool yields coin-scoped text/numbers we cannot get as cleanly from REST.  
   - **Hold** if MCP only duplicates quotes we already have → keep REST, archive epic or park M2+.

---

## 7. Risks

| Risk | Mitigation |
|------|------------|
| Plan does not include MCP / 403 | M1 fail-fast; epic stays backlog |
| Credits higher than REST | Cap symbols/cycle; cache tool results TTL |
| Tool schema churn | Thin client; map layer isolated |
| Double facts (Pro + MCP) | Prefer one primary per cycle or dedupe by day+type+symbol |
| Security | Key only on Hermes; no ledger collections |

---

## 8. Success criteria (epic done)

- [ ] M1 written and decision Go/Hold recorded on #107  
- [ ] If Go: Hermes can pull ≥1 MCP tool into `memory_market_events` with kill-switch  
- [ ] DCA/policy path unchanged (consumes same flags)  
- [ ] REST Pro still works as fallback or parallel  
- [ ] Staging soak note (OK / issues)  
- [ ] No Grok on evaluate hot path; no orders/positions writes  

---

## 9. Suggested next action

**Do M1 only** (half day–day): inventory + mapping doc under `plans/cmc-mcp-tools-inventory.md`, comment on #107.  
No production code until Go.

---

## 10. Links

- Epic: #107  
- D8 layer: #103 (closed)  
- REST provider: #105 (closed)  
- Product: https://coinmarketcap.com/api/mcp/  
- D8 plan: `plans/coin-fact-layer-d8.md`
