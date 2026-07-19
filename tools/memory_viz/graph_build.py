"""Build knowledge-graph edges from memory nodes + embeddings (pure, no I/O).

Edges = kNN cosine + same-symbol co-membership + same-lobe soft links.
Inspired by Cartographer (synapse) + GraphAura (typed force graph).
"""

from __future__ import annotations

from typing import Any, Sequence

from tools.memory_viz.ranking import cosine


def _sym_key(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    return s.split("/")[0]


def build_memory_graph(
    nodes: Sequence[dict[str, Any]],
    vectors: Sequence[Sequence[float]],
    *,
    knn: int = 5,
    min_sim: float = 0.12,
    max_nodes: int = 800,
    max_links: int = 4000,
    symbol_weight: float = 0.55,
    lobe_weight: float = 0.22,
) -> dict[str, Any]:
    """Return {nodes, links, stats} for Graph mode.

    Node fields kept for UI; links: {source, target, weight, kind}.
    Indices in links refer to position in returned nodes list (0..n-1).
    """
    n_all = min(len(nodes), len(vectors))
    if n_all == 0:
        return {
            "nodes": [],
            "links": [],
            "stats": {"node_count": 0, "link_count": 0, "knn": knn, "min_sim": min_sim},
        }

    # Prefer newest if over cap (nodes usually oldest-first; take tail)
    if n_all > max_nodes:
        start = n_all - max_nodes
        idx_map = list(range(start, n_all))
    else:
        idx_map = list(range(n_all))

    out_nodes: list[dict[str, Any]] = []
    vecs: list[list[float]] = []
    for new_i, old_i in enumerate(idx_map):
        n = dict(nodes[old_i])
        n["i"] = new_i
        n["gi"] = new_i  # graph index
        out_nodes.append(n)
        vecs.append(list(vectors[old_i]))

    n = len(out_nodes)
    link_map: dict[tuple[int, int], dict[str, Any]] = {}

    def add_link(a: int, b: int, weight: float, kind: str) -> None:
        if a == b:
            return
        if a > b:
            a, b = b, a
        key = (a, b)
        w = max(0.0, min(1.0, float(weight)))
        prev = link_map.get(key)
        if prev is None or w > float(prev.get("weight") or 0):
            link_map[key] = {
                "source": a,
                "target": b,
                "source_id": str(out_nodes[a].get("id") or ""),
                "target_id": str(out_nodes[b].get("id") or ""),
                "weight": round(w, 4),
                "kind": kind if prev is None else _merge_kind(str(prev.get("kind")), kind),
            }
        elif prev and kind not in str(prev.get("kind")):
            prev["kind"] = _merge_kind(str(prev.get("kind")), kind)
            prev["weight"] = round(max(float(prev["weight"]), w), 4)

    # --- kNN cosine ---
    k = max(1, min(int(knn), max(1, n - 1)))
    for i in range(n):
        scored: list[tuple[float, int]] = []
        vi = vecs[i]
        for j in range(n):
            if i == j:
                continue
            s = cosine(vi, vecs[j])
            if s >= min_sim:
                scored.append((s, j))
        scored.sort(key=lambda t: t[0], reverse=True)
        for s, j in scored[:k]:
            add_link(i, j, s, "semantic")

    # --- same symbol (trading co-membership) ---
    by_sym: dict[str, list[int]] = {}
    for i, node in enumerate(out_nodes):
        sk = _sym_key(str(node.get("symbol") or ""))
        if not sk:
            continue
        by_sym.setdefault(sk, []).append(i)
    for ids in by_sym.values():
        if len(ids) < 2:
            continue
        # star to first + chain to avoid O(n^2) explosion
        hub = ids[0]
        for j in ids[1: min(len(ids), 24)]:
            add_link(hub, j, symbol_weight, "symbol")
        for a, b in zip(ids, ids[1:]):
            add_link(a, b, symbol_weight * 0.85, "symbol")

    # --- same lobe soft mesh (sample) ---
    by_lobe: dict[str, list[int]] = {}
    for i, node in enumerate(out_nodes):
        lobe = str(node.get("lobe") or "other")
        by_lobe.setdefault(lobe, []).append(i)
    for ids in by_lobe.values():
        if len(ids) < 3:
            continue
        step = max(1, len(ids) // 12)
        sample = ids[::step][:16]
        for a, b in zip(sample, sample[1:]):
            add_link(a, b, lobe_weight, "lobe")

    links = sorted(link_map.values(), key=lambda L: -float(L["weight"]))[: int(max_links)]

    # degree for sizing
    deg = [0] * n
    for L in links:
        deg[int(L["source"])] += 1
        deg[int(L["target"])] += 1
    for i, node in enumerate(out_nodes):
        node["degree"] = deg[i]
        node["val"] = 1 + min(12, deg[i])  # force-graph size hint

    return {
        "nodes": out_nodes,
        "links": links,
        "stats": {
            "node_count": n,
            "link_count": len(links),
            "knn": k,
            "min_sim": min_sim,
            "kinds": _kind_counts(links),
        },
    }


def links_for_new_node(
    new_index: int,
    nodes: Sequence[dict[str, Any]],
    vectors: Sequence[Sequence[float]],
    *,
    knn: int = 5,
    min_sim: float = 0.12,
) -> list[dict[str, Any]]:
    """Links from a newly appended node (index) to existing ones."""
    n = min(len(nodes), len(vectors))
    if new_index < 0 or new_index >= n or n < 2:
        return []
    vi = list(vectors[new_index])
    scored: list[tuple[float, int]] = []
    for j in range(n):
        if j == new_index:
            continue
        s = cosine(vi, vectors[j])
        if s >= min_sim:
            scored.append((s, j))
    scored.sort(key=lambda t: t[0], reverse=True)
    nid = str(nodes[new_index].get("id") or "")
    links = []
    for s, j in scored[: max(1, knn)]:
        a, b = (j, new_index) if j < new_index else (new_index, j)
        links.append(
            {
                "source": a,
                "target": b,
                "source_id": str(nodes[a].get("id") or ""),
                "target_id": str(nodes[b].get("id") or ""),
                "weight": round(float(s), 4),
                "kind": "semantic",
            }
        )
    # symbol links
    sk = _sym_key(str(nodes[new_index].get("symbol") or ""))
    if sk:
        for j in range(n):
            if j == new_index:
                continue
            if _sym_key(str(nodes[j].get("symbol") or "")) == sk:
                a, b = (j, new_index) if j < new_index else (new_index, j)
                links.append(
                    {
                        "source": a,
                        "target": b,
                        "source_id": str(nodes[a].get("id") or ""),
                        "target_id": str(nodes[b].get("id") or ""),
                        "weight": 0.55,
                        "kind": "symbol",
                    }
                )
                if len([L for L in links if L["kind"] == "symbol"]) >= 8:
                    break
    return links


def _merge_kind(a: str, b: str) -> str:
    parts = set((a or "").split("+")) | set((b or "").split("+"))
    parts.discard("")
    order = ["semantic", "symbol", "lobe"]
    return "+".join([k for k in order if k in parts] or sorted(parts))


def _kind_counts(links: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for L in links:
        k = str(L.get("kind") or "other")
        out[k] = out.get(k, 0) + 1
    return out
