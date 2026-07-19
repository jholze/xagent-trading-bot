/**
 * Dual-mode Memory Viz: Cortex (point cloud) + Graph (force synapses).
 */
import { CortexScene } from "./scene.js";
import { GraphScene } from "./scene_graph.js";
import { Hud } from "./hud.js";

const cortexCanvas = document.getElementById("cortex-canvas");
const graphCanvas = document.getElementById("graph-canvas");
const cortex = new CortexScene(cortexCanvas);
const graph = new GraphScene(graphCanvas);

let mode = "cortex"; // cortex | graph
let lastHitScores = new Map();
let cortexNodes = [];
let graphData = { nodes: [], links: [] };
let ws = null;
let wsRetry = 0;

const hud = new Hud({
  onQuery: runQuery,
  onClear: () => {
    cortex.clearHits();
    graph.clearHits();
    lastHitScores = new Map();
    hud.setStatus("hits cleared");
  },
  onResetView: () => {
    activeScene().resetCamera();
    hud.setZoomSlider(activeScene().getZoomSliderValue());
  },
  onLobeToggle: (lobe, on) => {
    cortex.setLobeEnabled(lobe, on);
    graph.setLobeEnabled(lobe, on);
  },
  onSymbolToggle: () => {},
  onHitClick: async (index, id) => {
    if (mode === "graph") {
      graph.highlightByIds([id]);
    } else {
      cortex.selectIndex(index);
    }
    await openNode(id, lastHitScores.get(index));
  },
  onZoomIn: () => {
    activeScene().zoomIn();
    hud.setZoomSlider(activeScene().getZoomSliderValue());
  },
  onZoomOut: () => {
    activeScene().zoomOut();
    hud.setZoomSlider(activeScene().getZoomSliderValue());
  },
  onZoomSlider: (v) => {
    activeScene().setZoomFromSlider(v);
  },
  onModeChange: (m) => setMode(m),
});

hud.hooks.onSymbolsChanged = (activeList) => {
  const set = !activeList || !activeList.length ? null : new Set(activeList);
  cortex.setSymbolFilter(set);
  graph.setSymbolFilter(set);
};

function activeScene() {
  return mode === "graph" ? graph : cortex;
}

function setMode(m) {
  mode = m === "graph" ? "graph" : "cortex";
  document.body.dataset.vizMode = mode;
  cortex.setVisible?.(mode === "cortex");
  // CortexScene may not have setVisible — handle canvas
  cortexCanvas.style.display = mode === "cortex" ? "block" : "none";
  graph.setVisible(mode === "graph");
  hud.setModeUI(mode);
  hud.setZoomSlider(activeScene().getZoomSliderValue());
  if (mode === "graph" && (!graphData.nodes || !graphData.nodes.length)) {
    loadGraph();
  }
  const label = mode === "graph" ? "GRAPH" : "CORTEX";
  hud.toast(`${label} mode`);
}

// pick handlers
cortex.onPick(async (index, node) => {
  if (!node || mode !== "cortex") return;
  await openNode(node.id, lastHitScores.get(index));
});
graph.onPick(async (index, node) => {
  if (!node || mode !== "graph") return;
  await openNode(node.id, lastHitScores.get(index));
});
graph.onHover((idx, node) => {
  const tip = document.getElementById("hover-tip");
  if (!tip) return;
  if (idx < 0 || !node) {
    tip.hidden = true;
    return;
  }
  tip.hidden = false;
  tip.textContent = `${node.symbol || node.lobe || ""} · ${node.title || node.id || ""}`.trim();
});

cortex.onZoomChange((v) => {
  if (mode === "cortex") hud.setZoomSlider(v);
});
graph.onZoomChange((v) => {
  if (mode === "graph") hud.setZoomSlider(v);
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
    const ids = hits.map((h) => h.id);
    if (mode === "graph") {
      graph.highlightByIds(ids);
    } else {
      cortex.setHits(indices);
      if (hits[0]) cortex.selectIndex(hits[0].i);
    }
    hud.setHits(hits);
    hud.setStatus(
      `query “${q}” · ${hits.length} hits · top ${
        hits[0] && hits[0].score != null ? hits[0].score.toFixed(3) : "—"
      }`
    );
  } catch (e) {
    hud.setStatus("query failed");
    hud.toast(String(e.message || e));
  }
}

async function loadGraph() {
  hud.setStatus("building graph…");
  try {
    const g = await fetch("/api/graph?knn=5&min_sim=0.12").then((r) => r.json());
    graphData = g;
    graph.setGraph(g);
    const st = g.stats || {};
    hud.setStatus(
      `GRAPH · ${st.node_count || 0} nodes · ${st.link_count || 0} links · live`
    );
    hud.setGraphStats(st);
  } catch (e) {
    hud.toast("graph load failed");
    console.error(e);
  }
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws`;
  try {
    ws = new WebSocket(url);
  } catch (e) {
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
    if (el) el.classList.remove("on");
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
    if (!msg || !msg.type || msg.type === "hello" || msg.type === "ping") return;
    if (msg.type === "nodes_added" && Array.isArray(msg.nodes) && msg.nodes.length) {
      const existing = new Set(cortexNodes.map((n) => n.id));
      const fresh = msg.nodes.filter((n) => n && n.id && !existing.has(n.id));
      if (!fresh.length) return;
      cortexNodes = cortexNodes.concat(fresh);
      // flash only — no detail drawer
      cortex.appendNodes(fresh);
      graph.appendNodes(fresh, msg.links || []);
      const label = fresh[0].title || fresh[0].id;
      hud.toast(`+${fresh.length} memory · ${String(label).slice(0, 40)}`);
      hud.setStatus(
        `${mode.toUpperCase()} · ${msg.node_count || cortexNodes.length} nodes · +${fresh.length} new`
      );
    }
  };
}

function scheduleWsRetry() {
  wsRetry += 1;
  setTimeout(connectWs, Math.min(15000, 800 * wsRetry));
}

async function boot() {
  hud.setStatus("loading…");
  try {
    // ensure cortex scene has setVisible polyfill via canvas
    if (!cortex.setVisible) {
      cortex.setVisible = (on) => {
        cortexCanvas.style.display = on ? "block" : "none";
      };
    }
    const health = await fetch("/api/health").then((r) => r.json());
    const cortexPayload = await fetch("/api/cortex").then((r) => r.json());
    cortexNodes = cortexPayload.nodes || [];
    const lobes = (cortexPayload.lobes || []).map((L) => L.id);
    cortex.setAllLobes(lobes);
    graph.setAllLobes(lobes);
    cortex.setNodes(cortexNodes);
    cortex.start();
    graph.start();
    hud.bindCortex({ ...cortexPayload, ...health });
    // restore preferred mode (default cortex)
    let startMode = "cortex";
    try {
      const sm = localStorage.getItem("memory_viz_mode");
      if (sm === "graph" || sm === "cortex") startMode = sm;
    } catch (_) {}
    await loadGraph().catch(() => {});
    setMode(startMode);
    hud.setZoomSlider(activeScene().getZoomSliderValue());
    const modeLabel = health.demo ? "DEMO" : "LIVE";
    const src = health.source || "demo";
    hud.setStatus(`${modeLabel}/${src} · ${health.node_count} nodes · ${startMode}`);
    connectWs();
  } catch (e) {
    hud.setStatus("boot failed");
    hud.toast(String(e.message || e));
    console.error(e);
  }
}

boot();
