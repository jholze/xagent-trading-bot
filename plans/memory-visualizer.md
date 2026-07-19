# Epic #88 — Memory Visualizer (RAG cortex)

> **Status:** **V2 shipped** · WebSocket live + Mongo poll · Railway  
> **Issue:** [#88](https://github.com/jholze/xagent-trading-bot/issues/88)  
> **Depends on:** #72 Agent Bus + RAG (closed, shipped)  
> **UX north star:** [Project Golem](https://github.com/CyberMagician/Project_Golem) — then **surpass** it for *trading memory* (not a generic wiki brain)

---

## 1. One-liner

**A cinematic 3D cortex of our trading memory: query it like `/ask`, watch the right neurons fire, click a node and read the fact — beautiful enough to demo, precise enough to debug RAG.**

Product bar: **it has to feel geil** — dark glass, neon lobes, smooth glow, instant query feedback. Ops tool with production-grade UI craft, not a gray debug page.

---

## 2. Why (and the emotional goal)

| Pain | Visualizer |
|------|------------|
| RAG is a black box | Spatial clusters by type / source / symbol |
| `/ask` feels random | Same embedder → top-k **ignites** in the cloud |
| Coin-facts vs lessons mixed | Color lobes + filters |
| Staging hard to inspect | Browse without Mongo shell |
| “Show me the brain” | Demo-ready screenshot / short clip |

**Audience:** us (ops, demos). Not end-user trading UI. Still: craft level = portfolio piece.

---

## 3. Reference: Project Golem → Xagent Cortex

[CyberMagician/Project_Golem](https://github.com/CyberMagician/Project_Golem):

| Layer | Golem | Xagent Cortex |
|-------|--------|----------------|
| Idea | UMAP 3D + query glow | **Yes — baseline** |
| Vibe | Green terminal / wiki lobes | **Trading night desk:** cyan / amber / magenta / void black |
| Embeddings | Gemma + torch | Stored vectors + `embed_for_rag` |
| Store | Lance + npy | Mongo `memory_rag_chunks` |
| Query UX | Bar + top indices | Bar + **live score strip** + **detail drawer** + symbol chips |
| Nodes | Points only | Points + **soft bloom** + optional synapse edges on hover |
| Empty state | n/a | Seed demo cortex if Mongo empty (synthetic clusters) so UI always demos |

**Decision:** Golem is inspiration. We ship **our own** frontend under `tools/memory_viz/` — no torch runtime dependency.

---

## 4. UX / UI spec (“geil” checklist)

Ship V1 UI only if these land. Polish is not a later phase — it is the product.

### 4.1 Look & motion

| Element | Spec |
|---------|------|
| Background | Near-black `#05060a` + subtle radial vignette; optional slow particle dust (low count) |
| Nodes | Instanced points / small spheres; size ∝ mild score or recency |
| Lobes | Distinct neon palettes (see §6); slight emissive material |
| Query hit | Top-k **pulse + bloom**; non-hits dim to ~15% opacity |
| Edges | KNN synapses: hidden by default; on hover or “show graph” — thin cyan arcs |
| Camera | OrbitControls; auto-idle slow rotation when idle 8s; “reset view” button |
| Performance | Target 60fps @ 3k nodes (InstancedMesh / Points); no per-frame GC |

### 4.2 Chrome (HUD)

```text
┌─ XAGENT MEMORY CORTEX ────────────────────────── status: LIVE · N nodes ─┐
│  [  query the memory…                          ]  ⏎  top_k ▾          │
│  chips: ARIA  ZBT  TRX  ·  lobes: facts trades lessons events social     │
├────────────────────────────────────────────────────┬────────────────────┤
│                                                    │  NODE DETAIL       │
│              3D CORTEX (full bleed)                 │  symbol · source   │
│                                                    │  type · score      │
│                                                    │  text body…        │
│                                                    │  created_at        │
│                                                    │  [copy id]         │
├────────────────────────────────────────────────────┴────────────────────┤
│  HIT STRIP:  #1 0.91 ARIA volume_breakout · #2 0.87 lesson …            │
└─────────────────────────────────────────────────────────────────────────┘
```

| Control | Behavior |
|---------|----------|
| Query input | Enter → POST `/api/query` → animate hits within ~200ms |
| Lobe toggles | Show/hide color groups without re-export |
| Symbol chips | Filter to open/watchlist symbols from cortex meta |
| Click node | Open detail drawer (full text, meta JSON collapse) |
| Double-click | Fly camera to node |
| Legend | Bottom-right glowing dots (Golem-class) |
| Keyboard | `/` focus query · `Esc` clear hits · `F` focus selection |

### 4.3 Feel bar (must pass)

1. Open URL → **wow in &lt;2s** (cortex visible, no white flash)  
2. Type `ARIA volume` → cloud **ignites** with smooth fade  
3. Click a lit node → drawer with **readable** trading prose  
4. Toggle “coin_facts only” → other lobes ghost out  
5. Looks good in a Telegram screenshot / screen recording  

### 4.4 Empty / demo mode

If Mongo empty or `MEMORY_VIZ_DEMO=1`: generate **synthetic cortex** (6 lobes, ~400 nodes, fake ARIA/ZBT texts) so the UI is always shippable and reviewable without staging secrets.

---

## 5. Architecture (export + UI in one vertical)

```text
┌──────────────────────────────────────────────────────────────────┐
│  tools/memory_viz/                                               │
│                                                                  │
│  server.py (aiohttp or Flask — thin, read-only)                  │
│    GET  /                 → SPA shell                            │
│    GET  /api/cortex       → cortex.json (nodes, colors, edges)   │
│    GET  /api/health       → node_count, embed_backend, built_at  │
│    POST /api/query        → { indices, scores, hits[] }          │
│    GET  /api/node/:id     → full text + metadata                 │
│                                                                  │
│  static/                                                         │
│    index.html · css/cortex.css · js/main.js · js/scene.js        │
│    (Three.js r16x via importmap — no heavy bundler required V1)  │
│                                                                  │
│  build_cortex.py                                                 │
│    Mongo memory_rag_chunks → UMAP → data/cortex.json + vectors   │
│    OR demo generator → same schema                               │
└──────────────────────────────────────────────────────────────────┘
         ▲
         │ read-only
    Mongo memory_rag_chunks   (staging / local)
```

**Query path:** load float matrix at server start → `embed_for_rag(query)` → cosine top-k → return indices matching cortex node order. Prefer stored embeddings; refuse mixed dims.

Hermes trading loop: **unchanged**. Visualizer is a sidecar tool, not in the bot evaluate path.

---

## 6. Visual taxonomy (lobes)

| Lobe | Color (neon) | Heuristic |
|------|----------------|-----------|
| `coin_facts` | `#22d3ee` cyan | `cmc_pro_*`, `cmc_mcp_*`, `cmc_ai_*`, kind coin_fact |
| `trades` | `#f472b6` pink | trade / fill / pnl chunks |
| `lessons` | `#a78bfa` violet | lesson, dca_lesson, reflector |
| `events` | `#fbbf24` amber | market events, regime, fusion |
| `social` | `#4ade80` green | social / community |
| `other` | `#64748b` slate | fallback |

UI filters mirror these keys exactly.

---

## 7. Data schema (`cortex.json`)

```json
{
  "version": 1,
  "built_at": "2026-07-19T12:00:00Z",
  "embedding_backend": "hash",
  "embedding_dim": 384,
  "nodes": [
    {
      "i": 0,
      "id": "rag_abc…",
      "pos": [0.12, -0.4, 0.88],
      "col": [0.13, 0.83, 0.93],
      "lobe": "coin_facts",
      "symbol": "ARIA/USDT",
      "source": "cmc_pro_quotes",
      "type": "volume_breakout",
      "title": "ARIA +29% 24h volume breakout",
      "preview": "CMC Pro: ARIA +29.5% 24h…",
      "created_at": "…",
      "nbs": [3, 7, 11]
    }
  ]
}
```

Full `text` may live in `nodes_text.json` or server-side only (`/api/node/:id`) to keep first paint light.

---

## 8. Config

```json
"memory": {
  "visualizer": {
    "enabled": false,
    "max_chunks": 3000,
    "umap_neighbors": 30,
    "query_top_k": 40,
    "embedding_backend": "hash",
    "export_dir": "data/memory_cortex",
    "serve_host": "127.0.0.1",
    "serve_port": 8765,
    "demo_if_empty": true,
    "bloom_strength": 1.0,
    "idle_rotate": true
  }
}
```

Default **off** in bot config. Run via:

```bash
python -m tools.memory_viz.build_cortex   # or --demo
python -m tools.memory_viz.server         # http://127.0.0.1:8765
```

---

## 9. Code surface (UI-first — ship together)

| Path | Role |
|------|------|
| `tools/memory_viz/__init__.py` | package |
| `tools/memory_viz/lobes.py` | pure: metadata → lobe + RGB |
| `tools/memory_viz/build_cortex.py` | export UMAP / demo generator |
| `tools/memory_viz/server.py` | static + `/api/*` read-only |
| `tools/memory_viz/query.py` | cosine top-k over matrix |
| `tools/memory_viz/static/index.html` | shell + importmap |
| `tools/memory_viz/static/css/cortex.css` | HUD glass / neon |
| `tools/memory_viz/static/js/scene.js` | Three.js scene, bloom, hits |
| `tools/memory_viz/static/js/hud.js` | query, chips, drawer, hit strip |
| `tools/memory_viz/static/js/main.js` | boot |
| `tests/unit/test_memory_viz_lobes.py` | lobe mapping |
| `tests/unit/test_memory_viz_query.py` | ranking fixtures |
| `requirements-memory-viz.txt` | umap-learn, scikit-learn, numpy (+ flask/aiohttp); **no torch** |

Optional later: `intelligence/memory/OPS.md` short “Cortex” section.

---

## 10. Milestones (UI not deferred)

| ID | Title | Deliverable |
|----|--------|-------------|
| **V1** | **Vertical slice: demo UI + demo cortex** | Full Three.js HUD, glow query, detail drawer, synthetic data — **must pass §4.3** |
| **V2** | Live export from Mongo | `build_cortex` from `memory_rag_chunks`; dim guard; real lobes |
| **V3** | Retrieve parity | `/api/query` uses same `embed_for_rag` as Hermes; hit strip shows score + preview |
| **V4** | Staging ops | Runbook: env, rebuild, bind `127.0.0.1` or basic auth; screenshot in issue |
| **V5** | Juice (optional) | Weaviate parity badge, synapse mode, symbol fly-to, export short WebM via instructions |

**Order change vs earlier sketch:** UI is **V1**, not V3. Inventory happens inside V2 export, not as a docs-only gate.

### V1 acceptance (UI)

- [ ] `python -m tools.memory_viz.server --demo` opens geil cortex  
- [ ] Query animation + dim non-hits  
- [ ] Click → detail drawer with text  
- [ ] Lobe legend + toggles  
- [ ] No ledger imports; no trading modules on critical path  

### V2 acceptance

- [ ] Export ≤ max_chunks from Mongo  
- [ ] Mixed dims rejected with clear error  
- [ ] Real ARIA (or portfolio) nodes visible if present in staging  

### V3 acceptance

- [ ] Query scores sensible vs manual cosine  
- [ ] Documented embed backend in `/api/health`  

### V4 acceptance

- [ ] OPS note; default bind localhost  
- [ ] Trading path untouched  

---

## 11. Risks

| Risk | Mitigation |
|------|------------|
| UI scope creep | §4.3 is the freeze bar; V5 is juice only |
| Dim mismatch | Pin backend per build; health exposes dim |
| UMAP slow | Cap 3k; cache cortex.json; demo mode for CI |
| Three.js perf | Points/InstancedMesh; no lights-per-node |
| Public leak | Bind 127.0.0.1; optional token query param later |
| Ledger | Export allowlist `memory_rag_chunks` only |

---

## 12. Success criteria (epic done)

- [ ] Demo mode alone is screenshot-worthy  
- [ ] Staging cortex from real RAG + query glow for `ARIA` / `DCA` / `volume breakout`  
- [ ] Detail drawer shows real chunk text  
- [ ] No ledger writes; Hermes evaluate path unchanged  
- [ ] Short runbook in plan or OPS.md  

---

## 13. Suggested next action

**Implement V1 immediately:** package `tools/memory_viz/` with demo cortex + full HUD Three.js UI + `/api/query` on synthetic vectors.  
Then V2 wire Mongo. Do **not** wait for a separate inventory ticket.

---

## 14. Links

- Epic: #88  
- RAG bus: #72 (closed)  
- Golem (UX only): https://github.com/CyberMagician/Project_Golem  
- Memory ops: `intelligence/memory/OPS.md`  
- Hermes RAG: `HERMES_DOKUMENTATION.md`  
