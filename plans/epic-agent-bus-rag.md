# Epic: Agent Bus + Minimal Hermes RAG

> **GitHub:** [#72](https://github.com/jholze/xagent-trading-bot/issues/72)  
> **Status:** **CLOSED** completed (2026-07-18) — MVP shipped  
> **Orchestration:** own bus + packets — **LangChain non-goal** as spine  
> **North-star Q&A:** DCA last trades for coin X → add? how much? (advisory only)

## Shipped modules

| Module | Role |
|--------|------|
| `intelligence/memory/rag_config.py` | kill-switches |
| `intelligence/memory/rag_store.py` | `memory_rag_chunks` + in-memory backend |
| `intelligence/memory/rag_index.py` | index lessons/trades/events + **fusion snapshot (C8)** |
| `hermes/memory/rag_retriever.py` | add / retrieve / build_rag_prompt |
| `bus/schemas.py` | `RagQuery` / `RagResult` |
| `bus/publisher.py` | optional publish (default off) |
| `hermes/self_improver.py` | RAG section before Grok |
| `services/telegram_ask_bridge.py` | evidence-grounded `/ask` |
| `intelligence/strategy_discovery.py` | Grok prompt + RETRIEVED_MEMORY |
| `services/market_context_observability.py` | C8 hook on fusion state change |
| `config.json` → `memory.rag` | flags (`index_market_context` default false) |
| `requirements-rag.txt` | optional MiniLM for hermes image |

## Children

| Child | Issue | Status |
|-------|-------|--------|
| C1 | #73 | shipped |
| C2 | #74 | shipped |
| C3 | #75 | shipped |
| C4 | #76 | shipped (ask + strategy_discovery) |
| C5 | #77 | **shipped** MemoryRagChunk dim 384 + dual-write/search |
| C6 | #78 | closed completed |
| C7 | #80 | **shipped** pluggable llm_client (xai / openai_compat) |
| C8 | #81 | closed completed (default off) |

## Related

- #65 Decision Agents · #79 DCA policy · #71 Memory×rotation · #67 Cash
