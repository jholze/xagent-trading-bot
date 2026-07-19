/**
 * HUD: compact/expand settings, query, lobes, hits, drawer.
 */

const LOBE_CSS = {
  coin_facts: "#22d3ee",
  trades: "#f472b6",
  lessons: "#a78bfa",
  events: "#fbbf24",
  social: "#4ade80",
  other: "#64748b",
};

const PREF_KEY = "memory_viz_hud_mode";

export class Hud {
  /**
   * @param {{
   *  onQuery: (q: string, topK: number) => Promise<void>,
   *  onClear: () => void,
   *  onResetView: () => void,
   *  onLobeToggle: (lobe: string, on: boolean) => void,
   *  onSymbolToggle: (sym: string, on: boolean) => void,
   *  onHitClick: (index: number, id: string) => void,
   *  onZoomIn?: () => void,
   *  onZoomOut?: () => void,
   *  onZoomSlider?: (value: number) => void,
   * }} hooks
   */
  constructor(hooks) {
    this.hooks = hooks;
    this.lobeState = {};
    this.symbolState = {};
    this._nodeByIndex = [];
    this.compact = true;

    this.$panel = document.getElementById("hud-top");
    this.$status = document.getElementById("status-line");
    this.$input = document.getElementById("query-input");
    this.$inputCompact = document.getElementById("query-input-compact");
    this.$btn = document.getElementById("query-btn");
    this.$btnCompact = document.getElementById("query-btn-compact");
    this.$topK = document.getElementById("top-k");
    this.$hits = document.getElementById("hit-list");
    this.$lobes = document.getElementById("lobe-toggles");
    this.$chips = document.getElementById("symbol-chips");
    this.$legend = document.getElementById("legend");
    this.$drawer = document.getElementById("detail-drawer");
    this.$drawerMeta = document.getElementById("drawer-meta");
    this.$drawerTitle = document.getElementById("drawer-title");
    this.$drawerBody = document.getElementById("drawer-body");
    this.$copy = document.getElementById("copy-id");
    this.$toast = document.getElementById("toast");
    this.$expand = document.getElementById("hud-expand");
    this.$expanded = document.getElementById("hud-expanded");
    this.$zoomIn = document.getElementById("zoom-in");
    this.$zoomOut = document.getElementById("zoom-out");
    this.$zoomSlider = document.getElementById("zoom-slider");
    this._copyId = "";

    // default compact
    let mode = "compact";
    try {
      const saved = localStorage.getItem(PREF_KEY);
      if (saved === "expanded" || saved === "compact") mode = saved;
    } catch (_) {}
    this.setCompact(mode !== "expanded");

    this.$expand.addEventListener("click", () => {
      this.setCompact(!this.compact);
    });

    const run = () => this._runQuery();
    this.$btn.addEventListener("click", run);
    this.$btnCompact.addEventListener("click", run);
    this.$input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") run();
    });
    this.$inputCompact.addEventListener("keydown", (e) => {
      if (e.key === "Enter") run();
    });
    // keep query fields in sync
    this.$input.addEventListener("input", () => {
      this.$inputCompact.value = this.$input.value;
    });
    this.$inputCompact.addEventListener("input", () => {
      this.$input.value = this.$inputCompact.value;
    });

    document.getElementById("clear-hits").addEventListener("click", () => {
      this.hooks.onClear();
      this.setHits([]);
    });
    document.getElementById("reset-view").addEventListener("click", () => this.hooks.onResetView());
    document.getElementById("drawer-close").addEventListener("click", () => {
      this.$drawer.hidden = true;
    });
    this.$copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(this._copyId || "");
        this.toast("id copied");
      } catch {
        this.toast(this._copyId || "");
      }
    });

    this.$zoomIn.addEventListener("click", () => this.hooks.onZoomIn && this.hooks.onZoomIn());
    this.$zoomOut.addEventListener("click", () => this.hooks.onZoomOut && this.hooks.onZoomOut());
    this.$zoomSlider.addEventListener("input", () => {
      const v = parseFloat(this.$zoomSlider.value);
      if (this.hooks.onZoomSlider) this.hooks.onZoomSlider(v);
    });

    window.addEventListener("keydown", (e) => {
      if (e.key === "/" && document.activeElement !== this.$input && document.activeElement !== this.$inputCompact) {
        e.preventDefault();
        if (this.compact) this.$inputCompact.focus();
        else this.$input.focus();
      }
      if (e.key === "Escape") {
        this.hooks.onClear();
        this.setHits([]);
        this.$drawer.hidden = true;
      }
      if (e.key === "+" || e.key === "=") {
        if (document.activeElement?.tagName === "INPUT") return;
        e.preventDefault();
        this.hooks.onZoomIn && this.hooks.onZoomIn();
      }
      if (e.key === "-" || e.key === "_") {
        if (document.activeElement?.tagName === "INPUT") return;
        e.preventDefault();
        this.hooks.onZoomOut && this.hooks.onZoomOut();
      }
    });
  }

  setCompact(compact) {
    this.compact = !!compact;
    document.body.classList.toggle("hud-compact", this.compact);
    document.body.classList.toggle("hud-expanded", !this.compact);
    this.$panel.classList.toggle("compact", this.compact);
    this.$panel.classList.toggle("expanded", !this.compact);
    this.$panel.dataset.mode = this.compact ? "compact" : "expanded";
    this.$expanded.hidden = this.compact;
    this.$expand.setAttribute("aria-expanded", this.compact ? "false" : "true");
    this.$expand.title = this.compact ? "Expand settings" : "Collapse settings";
    this.$expand.textContent = this.compact ? "☰" : "▴";
    try {
      localStorage.setItem(PREF_KEY, this.compact ? "compact" : "expanded");
    } catch (_) {}
  }

  setZoomSlider(value) {
    if (this.$zoomSlider) this.$zoomSlider.value = String(Math.round(value));
  }

  setStatus(text) {
    this.$status.textContent = text;
  }

  toast(msg) {
    this.$toast.hidden = false;
    this.$toast.textContent = msg;
    clearTimeout(this._toastT);
    this._toastT = setTimeout(() => {
      this.$toast.hidden = true;
    }, 1800);
  }

  bindCortex(cortex) {
    this._nodeByIndex = cortex.nodes || [];
    const lobes = cortex.lobes || [];
    this.$lobes.innerHTML = "";
    this.$legend.innerHTML = "";
    this.lobeState = {};

    for (const L of lobes) {
      const id = L.id;
      this.lobeState[id] = true;
      const color = LOBE_CSS[id] || "#94a3b8";
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip on";
      chip.dataset.lobe = id;
      chip.style.color = color;
      chip.textContent = (L.label || id).replace("_", " ");
      chip.addEventListener("click", () => {
        this.lobeState[id] = !this.lobeState[id];
        chip.classList.toggle("on", this.lobeState[id]);
        chip.classList.toggle("off", !this.lobeState[id]);
        this.hooks.onLobeToggle(id, this.lobeState[id]);
      });
      this.$lobes.appendChild(chip);

      const row = document.createElement("div");
      row.className = "legend-row";
      row.innerHTML = `<span>${(L.label || id).replace("_", " ")}</span><span class="legend-dot" style="background:${color};color:${color}"></span>`;
      this.$legend.appendChild(row);
    }

    const symbols = new Set();
    for (const n of this._nodeByIndex) {
      const s = (n.symbol || "").split("/")[0];
      if (s) symbols.add(s);
    }
    this.$chips.innerHTML = "";
    this.symbolState = {};
    for (const s of [...symbols].sort()) {
      this.symbolState[s] = false;
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = s;
      chip.addEventListener("click", () => {
        this.symbolState[s] = !this.symbolState[s];
        chip.classList.toggle("on", this.symbolState[s]);
        const active = Object.entries(this.symbolState)
          .filter(([, v]) => v)
          .map(([k]) => k);
        this.hooks.onSymbolToggle(s, this.symbolState[s]);
        if (this.hooks.onSymbolsChanged) {
          this.hooks.onSymbolsChanged(active);
        }
      });
      this.$chips.appendChild(chip);
    }

    const mode = cortex.demo ? "DEMO" : "LIVE";
    this.setStatus(
      `${mode} · ${cortex.node_count ?? this._nodeByIndex.length} nodes`
    );
  }

  setHits(hits) {
    this.$hits.innerHTML = "";
    if (!hits || !hits.length) {
      this.$hits.innerHTML = `<span class="muted">No hits</span>`;
      return;
    }
    hits.slice(0, 24).forEach((h, rank) => {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "hit-item";
      const sc = typeof h.score === "number" ? h.score.toFixed(3) : "?";
      const label = h.title || h.preview || h.id || "";
      el.innerHTML = `<span class="sc">#${rank + 1} ${sc}</span>${escapeHtml(String(label).slice(0, 48))}`;
      el.title = h.preview || label;
      el.addEventListener("click", () => this.hooks.onHitClick(h.i, h.id));
      this.$hits.appendChild(el);
    });
  }

  showNode(node, score) {
    if (!node) return;
    this.$drawer.hidden = false;
    this._copyId = node.id || "";
    const rows = [
      ["id", node.id],
      ["lobe", node.lobe],
      ["symbol", node.symbol || "—"],
      ["source", node.source],
      ["type", node.type],
      ["score", score != null ? Number(score).toFixed(4) : "—"],
      ["created", node.created_at || "—"],
    ];
    this.$drawerMeta.innerHTML = rows
      .map(([k, v]) => `<span class="k">${k}</span><span class="v">${escapeHtml(String(v ?? "—"))}</span>`)
      .join("");
    this.$drawerTitle.textContent = node.title || node.id || "";
    this.$drawerBody.textContent = node.text || node.preview || "";
  }

  async _runQuery() {
    const q = (
      (this.compact ? this.$inputCompact.value : this.$input.value) ||
      this.$input.value ||
      this.$inputCompact.value ||
      ""
    ).trim();
    this.$input.value = q;
    this.$inputCompact.value = q;
    const topK = parseInt(this.$topK.value || "40", 10) || 40;
    if (!q) {
      this.toast("enter a query");
      return;
    }
    this.setStatus("querying…");
    await this.hooks.onQuery(q, topK);
  }
}

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
