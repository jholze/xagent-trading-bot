/**
 * Boot Memory Cortex UI — compact HUD, zoom, WS live flash (no auto-drawer).
 */
import { CortexScene } from "./scene.js";
import { Hud } from "./hud.js";

const canvas = document.getElementById("cortex-canvas");
const scene = new CortexScene(canvas);

let lastHitScores = new Map();
let cortexNodes = [];
let ws = null;
let wsRetry = 0;

const hud = new Hud({
  onQuery: runQuery,
  onClear: () => {
    scene.clearHits();
    lastHitScores = new Map();
    hud.setStatus("hits cleared");
  },
  onResetView: () => {
    scene.resetCamera();
    hud.setZoomSlider(scene.getZoomSliderValue());
  },
  onLobeToggle: (lobe, on) => scene.setLobeEnabled(lobe, on),
  onSymbolToggle: () => {},
  onHitClick: async (index, id) => {
    scene.selectIndex(index);
    await openNode(id, lastHitScores.get(index));
  },
  onZoomIn: () => {
    scene.zoomIn();
    hud.setZoomSlider(scene.getZoomSliderValue());
  },
  onZoomOut: () => {
    scene.zoomOut();
    hud.setZoomSlider(scene.getZoomSliderValue());
  },
  onZoomSlider: (v) => {
    scene.setZoomFromSlider(v);
  },
});

hud.hooks.onSymbolsChanged = (activeList) => {
  if (!activeList || !activeList.length) {
    scene.setSymbolFilter(null);
  } else {
    scene.setSymbolFilter(new Set(activeList));
  }
};

scene.onZoomChange((v) => hud.setZoomSlider(v));

// Click node → detail drawer (only intentional pick, not live ingest)
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
    hud.setStatus(
      `query “${q}” · ${hits.length} hits · top ${
        hits[0] && hits[0].score != null ? hits[0].score.toFixed(3) : "—"
      }`
    );
    if (hits[0]) {
      scene.selectIndex(hits[0].i);
    }
  } catch (e) {
    hud.setStatus("query failed");
    hud.toast(String(e.message || e));
  }
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws`;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    hud.toast("ws connect failed");
    scheduleWsRetry();
    return;
  }
  ws.onopen = () => {
    wsRetry = 0;
    const el = document.getElementById("ws-dot");
    if (el) {
      el.classList.add("on");
      el.title = "WebSocket live";
    }
  };
  ws.onclose = () => {
    const el = document.getElementById("ws-dot");
    if (el) {
      el.classList.remove("on");
      el.title = "WebSocket offline";
    }
    scheduleWsRetry();
  };
  ws.onerror = () => {
    try {
      ws.close();
    } catch (_) {}
  };
  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (!msg || !msg.type) return;
    if (msg.type === "hello" || msg.type === "ping") return;
    if (msg.type === "nodes_added" && Array.isArray(msg.nodes) && msg.nodes.length) {
      const existing = new Set(cortexNodes.map((n) => n.id));
      const fresh = msg.nodes.filter((n) => n && n.id && !existing.has(n.id));
      if (!fresh.length) return;
      cortexNodes = cortexNodes.concat(fresh);
      // flash only — no detail drawer
      scene.appendNodes(fresh);
      const label = fresh[0].title || fresh[0].id;
      hud.toast(`+${fresh.length} memory · ${String(label).slice(0, 40)}`);
      hud.setStatus(
        `LIVE · ${msg.node_count || cortexNodes.length} nodes · +${fresh.length} new`
      );
    }
  };
}

function scheduleWsRetry() {
  wsRetry += 1;
  const delay = Math.min(15000, 800 * wsRetry);
  setTimeout(connectWs, delay);
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
    hud.setZoomSlider(scene.getZoomSliderValue());
    const mode = health.demo ? "DEMO" : "LIVE";
    const src = health.source || (health.demo ? "demo" : "mongo");
    hud.setStatus(
      `${mode}/${src} · ${health.node_count} nodes · ws…`
    );
    connectWs();
  } catch (e) {
    hud.setStatus("boot failed — is the server running?");
    hud.toast(String(e.message || e));
    console.error(e);
  }
}

boot();
