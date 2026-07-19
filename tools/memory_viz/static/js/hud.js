/**
 * HUD: query, lobes, chips, hit strip, detail drawer.
 */

const LOBE_CSS = {
  coin_facts: "#22d3ee",
  trades: "#f472b6",
  lessons: "#a78bfa",
  events: "#fbbf24",
  social: "#4ade80",
  other: "#64748b",
};

export class Hud {
  /**
   * @param {{
   *  onQuery: (q: string, topK: number) => Promise<void>,
   *  onClear: () => void,
   *  onResetView: () => void,
   *  onLobeToggle: (lobe: string, on: boolean) => void,
   *  onSymbolToggle: (sym: string, on: boolean) => void,
   *  onHitClick: (index: number, id: string) => void,
   * }} hooks
   */
  constructor(hooks) {
    this.hooks = hooks;
    this.lobeState = {};
    this.symbolState = {};
    this._nodeByIndex = [];

    this.$status = document.getElementById("status-line");
    this.$input = document.getElementById("query-input");
    this.$btn = document.getElementById("query-btn");
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
    this._copyId = "";

    this.$btn.addEventListener("click", () => this._runQuery());
    this.$input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") this._runQuery();
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

    window.addEventListener("keydown", (e) => {
      if (e.key === "/" && document.activeElement !== this.$input) {
        e.preventDefault();
        this.$input.focus();
      }
      if (e.key === "Escape") {
        this.hooks.onClear();
        this.setHits([]);
        this.$drawer.hidden = true;
      }
    });
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

  /**
   * @param {{nodes: any[], lobes?: any[], demo?: boolean, node_count?: number, built_at?: string}} cortex
   */
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
      this.symbolState[s] = false; // off = not filtering; we use "active filters only if any on"
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
        // pass full set via custom: re-fire with aggregate
        if (this.hooks.onSymbolsChanged) {
          this.hooks.onSymbolsChanged(active);
        }
      });
      this.$chips.appendChild(chip);
    }

    const mode = cortex.demo ? "DEMO" : "LIVE";
    this.setStatus(
      `${mode} · ${cortex.node_count ?? this._nodeByIndex.length} nodes · built ${cortex.built_at || "—"}`
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
    const q = (this.$input.value || "").trim();
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
