/**
 * Boot Memory Cortex UI — load cortex, wire scene + HUD + APIs.
 */
import { CortexScene } from "./scene.js";
import { Hud } from "./hud.js";

const canvas = document.getElementById("cortex-canvas");
const scene = new CortexScene(canvas);

let lastHitScores = new Map();
let cortexNodes = [];

const hud = new Hud({
  onQuery: runQuery,
  onClear: () => {
    scene.clearHits();
    lastHitScores = new Map();
    hud.setStatus("hits cleared");
  },
  onResetView: () => scene.resetCamera(),
  onLobeToggle: (lobe, on) => scene.setLobeEnabled(lobe, on),
  onSymbolToggle: () => {},
  onHitClick: async (index, id) => {
    scene.selectIndex(index);
    await openNode(id, lastHitScores.get(index));
  },
});

// aggregate symbol filter
hud.hooks.onSymbolsChanged = (activeList) => {
  if (!activeList || !activeList.length) {
    scene.setSymbolFilter(null);
  } else {
    scene.setSymbolFilter(new Set(activeList));
  }
};

scene.onPick(async (index, node) => {
  if (!node) return;
  await openNode(node.id, lastHitScores.get(index));
});

async function openNode(id, score) {
  try {
    const res = await fetch(`/api/node/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error("node fetch failed");
    const node = await res.json();
    hud.showNode(node, score);
  } catch (e) {
    hud.toast(String(e.message || e));
  }
}

async function runQuery(q, topK) {
  try {
    const res = await fetch("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: q, top_k: topK }),
    });
    if (!res.ok) throw new Error(`query HTTP ${res.status}`);
    const data = await res.json();
    const hits = data.hits || [];
    const indices = data.indices || hits.map((h) => h.i);
    lastHitScores = new Map(hits.map((h) => [h.i, h.score]));
    scene.setHits(indices);
    hud.setHits(hits);
    hud.setStatus(`query “${q}” · ${hits.length} hits · top ${(hits[0] && hits[0].score != null) ? hits[0].score.toFixed(3) : "—"}`);
    if (hits[0]) {
      scene.selectIndex(hits[0].i);
    }
  } catch (e) {
    hud.setStatus("query failed");
    hud.toast(String(e.message || e));
  }
}

async function boot() {
  hud.setStatus("loading cortex…");
  try {
    const health = await fetch("/api/health").then((r) => r.json());
    const cortex = await fetch("/api/cortex").then((r) => r.json());
    cortexNodes = cortex.nodes || [];
    const lobes = (cortex.lobes || []).map((L) => L.id);
    scene.setAllLobes(lobes);
    scene.setNodes(cortexNodes);
    scene.start();
    hud.bindCortex({ ...cortex, ...health });
    hud.setStatus(
      `${health.demo ? "DEMO" : "LIVE"} · ${health.node_count} nodes · dim ${health.embedding_dim || "?"} · ready`
    );
  } catch (e) {
    hud.setStatus("boot failed — is the server running?");
    hud.toast(String(e.message || e));
    console.error(e);
  }
}

boot();
