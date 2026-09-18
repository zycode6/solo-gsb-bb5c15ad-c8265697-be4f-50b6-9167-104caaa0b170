"""证据链重建引擎。

模型说明
========
1. 每个来源 s 维护审计员可调整的时钟偏差假设：
       ref(t) = t + offset + drift*1e-6 * (t - t_ref)
   其中 t_ref 取全部记录中的最早源时钟时间戳。

2. 每条事件 e 在参考时间轴上维护一个可信区间：
       [ref(raw) - jitter - uncertainty,  ref(raw) + jitter + uncertainty]
   以及区间中点 ref_mid。

3. 先后约束来自两类证据：
   - explicit ：导入 JSON 里 before / after / predecessor 显式给出的因果链；
   - monotonic：当来源声明"时钟单调"时，同源事件按源时钟顺序串联。

4. 约束图先做 Tarjan 强连通分析，强连通环即"不可能顺序"冲突链；
   无环时按拓扑序向前传播最早界、逆拓扑序向后传播最晚界。
   传播后若下界超过上界，该事件的时间区间被约束挤压为空，属于冲突；
   每个环上的显式/单调边、以及冲突事件相关边标记为 impossible。

5. 可信度 = 0.65*来源信任 + 0.25*区间锐度 + 0.10*一致性。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from typing import Any

from .database import session

# Tarjan 是递归实现，审计数据可能较大；提高递归深度。
sys.setrecursionlimit(200_000)


# --------------------------------------------------------------------------- #
# 数据读取
# --------------------------------------------------------------------------- #
def _load_sources(conn) -> dict[int, dict[str, Any]]:
    sources: dict[int, dict[str, Any]] = {}
    for row in conn.execute("SELECT * FROM sources").fetchall():
        sources[row["id"]] = dict(row)
    return sources


def _load_events(conn) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id").fetchall()]


def _load_links(conn) -> set[tuple[int, int]]:
    return {(r["from_event"], r["to_event"]) for r in conn.execute("SELECT from_event, to_event FROM links")}


# --------------------------------------------------------------------------- #
# Tarjan 强连通分量（迭代实现，避免深图爆栈）
# --------------------------------------------------------------------------- #
def _tarjan_scc(adj: dict[int, set[int]], nodes: list[int]) -> list[list[int]]:
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    counter = 0
    sccs: list[list[int]] = []

    for root in nodes:
        if root in index:
            continue
        work: list[tuple[int, Any]] = [(root, iter(sorted(adj.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            v, it = work[-1]
            pushed = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(adj.get(w, ())))))
                    pushed = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if pushed:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                comp: list[int] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                sccs.append(comp)
    return sccs


# --------------------------------------------------------------------------- #
# 拓扑排序（Kahn，按 id 排序保证输出稳定）
# --------------------------------------------------------------------------- #
def _topo_order(nodes: set[int], adj: dict[int, set[int]]) -> tuple[list[int], set[int]]:
    indeg: dict[int, int] = {n: 0 for n in nodes}
    for u in nodes:
        for v in adj.get(u, ()):  # u -> v 表示 u 必须先于 v
            indeg[v] += 1
    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: list[int] = []
    while ready:
        u = ready.pop(0)
        order.append(u)
        for v in sorted(adj.get(u, ())):
            indeg[v] -= 1
            if indeg[v] == 0:
                # 保持按 id 的稳定次序
                lo, hi = 0, len(ready)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if ready[mid] < v:
                        lo = mid + 1
                    else:
                        hi = mid
                ready.insert(lo, v)
    return order, set(order)


# --------------------------------------------------------------------------- #
# 冲突链提取
# --------------------------------------------------------------------------- #
def _chain_for_cycle(comp: list[int], adj: dict[int, set[int]]) -> list[int]:
    """在一个强连通分量内还原一条闭合冲突链（含回到起点的闭环）。"""
    start = min(comp)
    in_comp = set(comp)
    chain = [start]
    seen_pairs: set[tuple[int, int]] = set()
    current = start
    while True:
        nxt = None
        for w in sorted(adj.get(current, ())):
            if w in in_comp and (current, w) not in seen_pairs:
                nxt = w
                break
        if nxt is None:  # 自环或退化情况
            break
        seen_pairs.add((current, nxt))
        chain.append(nxt)
        current = nxt
        if nxt == start:
            break
        if len(chain) > len(comp) + 2:
            break
    if chain[-1] != start and start in adj.get(current, ()):
        chain.append(start)
    elif chain[-1] != start:
        chain.append(start)
    return chain


def _pred_chain(v: int,
                lower_src: dict[int, int | None]) -> list[int]:
    """沿 lower 传播来源回溯，给出导致该事件下界被抬高的证据链。"""
    chain: list[int] = []
    visited: set[int] = set()
    cur: int | None = lower_src.get(v)
    while cur is not None and cur not in visited:
        visited.add(cur)
        chain.append(cur)
        cur = lower_src.get(cur)
    chain.reverse()
    chain.append(v)
    return chain


# --------------------------------------------------------------------------- #
# 主入口：重算全部派生结果
# --------------------------------------------------------------------------- #
def recompute(conn) -> dict[str, Any]:
    sources = _load_sources(conn)
    events = _load_events(conn)
    explicit_links = _load_links(conn)

    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM derived_constraints")

    if not events:
        conn.commit()
        return {
            "event_count": 0, "source_count": len(sources),
            "conflict_count": 0, "chain_count": 0,
            "warnings": [], "span_seconds": 0,
        }

    events_by_id = {e["id"]: e for e in events}

    # 1) 时钟偏差修正与初始可信区间 ----------------------------------------- #
    t_ref = min(e["raw_ts"] for e in events)
    for e in events:
        s = sources[e["source_id"]]
        mid = e["raw_ts"] + s["offset_seconds"] + s["drift_ppm"] * 1e-6 * (e["raw_ts"] - t_ref)
        half = e["jitter"] + s["uncertainty_seconds"]
        e["ref_mid"] = mid
        e["ref_lower"] = mid - half
        e["ref_upper"] = mid + half

    # 2) 构造先后约束图 ----------------------------------------------------- #
    adj: dict[int, set[int]] = defaultdict(set)
    edge_origin: dict[tuple[int, int], str] = {}

    # 2a) 同源单调：同源事件按 raw_ts（再按 id）排序后串联相邻事件
    by_source: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_source[e["source_id"]].append(e)
    for sid, evs in by_source.items():
        if not sources[sid]["monotonic"]:
            continue
        evs_sorted = sorted(evs, key=lambda x: (x["raw_ts"], x["id"]))
        for a, b in zip(evs_sorted, evs_sorted[1:]):
            if a["id"] != b["id"]:
                adj[a["id"]].add(b["id"])
                edge_origin[(a["id"], b["id"])] = "monotonic"

    # 2b) 显式因果链
    for a, b in explicit_links:
        if a in events_by_id and b in events_by_id and a != b:
            adj[a].add(b)
            edge_origin[(a, b)] = "explicit"

    node_set = set(events_by_id)
    for n in node_set:
        adj.setdefault(n, set())

    # 3) Tarjan 找冲突环 ---------------------------------------------------- #
    conflict_events: set[int] = set()
    conflict_rows: list[tuple[str, str, str]] = []
    import json as _json
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cycle_chains: list[list[int]] = []
    # 事件 id -> 所在冲突环 SCC 的编号（用于判定环内边）
    cycle_of: dict[int, int] = {}

    for cid, comp in enumerate(_tarjan_scc(adj, sorted(node_set))):
        is_cycle = len(comp) > 1 or (len(comp) == 1 and comp[0] in adj.get(comp[0], ()))
        if not is_cycle:
            continue
        chain = _chain_for_cycle(comp, adj)
        cycle_chains.append(chain)
        for n in comp:
            conflict_events.add(n)
            cycle_of[n] = cid
        labels = [
            f"{events_by_id[n]['ext_id'] or '#' + str(n)}"
            f"@{sources[events_by_id[n]['source_id']]['name']}"
            for n in chain
        ]
        detail = "因果约束形成闭环：" + " -> ".join(labels) + "（请核对显式 before/after 或关闭来源单调假设）"
        conflict_rows.append(("cycle", _json.dumps(chain), detail))

    # 4) 无环部分：拓扑传播收紧区间 ----------------------------------------- #
    acyclic_nodes = node_set - conflict_events
    sub_adj: dict[int, set[int]] = {n: {w for w in adj[n] if w in acyclic_nodes} for n in acyclic_nodes}
    order, _ordered = _topo_order(acyclic_nodes, sub_adj)
    # 已排除冲突环节点，剩余子图必然无环、可完整排序。

    lower = {n: events_by_id[n]["ref_lower"] for n in acyclic_nodes}
    upper = {n: events_by_id[n]["ref_upper"] for n in acyclic_nodes}
    lower_src: dict[int, int | None] = {n: None for n in acyclic_nodes}
    upper_src: dict[int, int | None] = {n: None for n in acyclic_nodes}

    # 前向：u -> v，则 lower[v] >= lower[u]
    for u in order:
        for v in sub_adj.get(u, ()):
            if lower[u] > lower[v]:
                lower[v] = lower[u]
                lower_src[v] = u
    # 反向：u -> v，则 upper[u] <= upper[v]
    for u in reversed(order):
        for v in sub_adj.get(u, ()):
            if upper[v] < upper[u]:
                upper[u] = upper[v]
                upper_src[u] = v

    interval_conflicts: set[int] = set()
    for n in acyclic_nodes:
        if lower[n] > upper[n] + 1e-9:
            interval_conflicts.add(n)
            conflict_events.add(n)

    # 为每个区间冲突生成证据链（同一根因会收敛到同一条链）
    seen_chains: set[tuple[int, ...]] = set()
    for n in sorted(interval_conflicts):
        chain = _pred_chain(n, lower_src)
        key = tuple(chain)
        if key in seen_chains:
            continue
        seen_chains.add(key)
        chain_labels = [
            f"{events_by_id[x]['ext_id'] or '#' + str(x)}"
            f"[{_fmt_ts(lower[x] if x in interval_conflicts or lower[x] > upper[x] + 1e-9 else events_by_id[x]['ref_lower'])},"
            f" {_fmt_ts(upper[x] if x in interval_conflicts else events_by_id[x]['ref_upper'])}]"
            for x in chain
        ]
        detail = (
            "先后约束把可信时间区间挤压为空："
            + " -> ".join(chain_labels)
            + f"（事件 {events_by_id[n]['ext_id'] or n} 下界 {_fmt_ts(lower[n])} 晚于上界 {_fmt_ts(upper[n])}，"
            "请放宽不确定度或调整时钟偏差假设）"
        )
        conflict_rows.append(("interval", _json.dumps(chain), detail))

    # 区间冲突也会污染其前后近邻（它们支撑了不可能的边界）
    for n in list(interval_conflicts):
        if lower_src[n] is not None:
            conflict_events.add(lower_src[n])
        if upper_src[n] is not None:
            conflict_events.add(upper_src[n])

    # 5) 边判定 forced / ambiguous / impossible ----------------------------- #
    impossible_edges: set[tuple[int, int]] = set()
    constraint_rows: list[tuple[int, int, str, str]] = []
    eps = 1e-9

    for (u, v), origin in edge_origin.items():
        if u not in events_by_id or v not in events_by_id:
            continue
        # 同一冲突环 SCC 内的边均不可能成立；跨出/跨入 SCC 的边不参与该环矛盾
        if u in cycle_of and cycle_of.get(u) == cycle_of.get(v):
            status = "impossible"
            impossible_edges.add((u, v))
        elif u in interval_conflicts or v in interval_conflicts:
            status = "impossible"
            impossible_edges.add((u, v))
        elif u in acyclic_nodes and v in acyclic_nodes:
            if upper[u] + eps < lower[v]:
                status = "forced"
            elif lower[v] + eps < upper[u]:  # 区间仍重叠 => 顺序不能确定
                status = "ambiguous"
            else:
                status = "forced"
        else:
            status = "ambiguous"
        constraint_rows.append((u, v, origin, status))

    # 6) 可信度 + 序号落库 --------------------------------------------------- #
    span_lo = min(events_by_id[n]["ref_lower"] for n in node_set)
    span_hi = max(events_by_id[n]["ref_upper"] for n in node_set)
    span = max(span_hi - span_lo, 1.0)
    max_half = max(e["jitter"] + sources[e["source_id"]]["uncertainty_seconds"] for e in events) or 1.0

    def _confidence(e: dict[str, Any]) -> float:
        s = sources[e["source_id"]]
        half = e["jitter"] + s["uncertainty_seconds"]
        sharp = max(0.0, 1.0 - half / max(max_half, 1e-9))
        consistency = 0.0 if e["id"] in conflict_events else 1.0
        score = 0.65 * s["trust"] + 0.25 * sharp + 0.10 * consistency
        return round(min(1.0, max(0.0, score)), 3)

    # 全局排序序号：按（下界, 中点, 上界, id）
    ordered_all = sorted(node_set, key=lambda n: (
        lower.get(n, events_by_id[n]["ref_lower"]),
        events_by_id[n]["ref_mid"],
        upper.get(n, events_by_id[n]["ref_upper"]),
        n,
    ))
    ordinal_of: dict[int, int] = {n: i for i, n in enumerate(ordered_all)}

    for e in events:
        n = e["id"]
        conn.execute(
            "UPDATE events SET ref_mid=?, ref_lower=?, ref_upper=?, confidence=?, ordinal=?, conflict_flag=? WHERE id=?",
            (
                events_by_id[n]["ref_mid"],
                lower.get(n, events_by_id[n]["ref_lower"]),
                upper.get(n, events_by_id[n]["ref_upper"]),
                _confidence(events_by_id[n]),
                ordinal_of[n],
                1 if n in conflict_events else 0,
                n,
            ),
        )

    for kind, ids_json, detail in conflict_rows:
        conn.execute(
            "INSERT INTO conflicts (kind, event_ids, detail, created_at) VALUES (?, ?, ?, ?)",
            (kind, ids_json, detail, now_iso),
        )
    for u, v, origin, status in constraint_rows:
        conn.execute(
            "INSERT INTO derived_constraints (from_event, to_event, origin, status) VALUES (?, ?, ?, ?)",
            (u, v, origin, status),
        )

    conn.commit()
    return {
        "event_count": len(events),
        "source_count": len(sources),
        "conflict_count": len(conflict_rows),
        "chain_count": len(conflict_rows),
        "conflict_event_count": len(conflict_events),
        "impossible_edge_count": len(impossible_edges),
        "span_seconds": span_hi - span_lo,
        "warnings": [],
    }


def recompute_default() -> dict[str, Any]:
    """使用默认数据库路径执行重算（供 CLI / entrypoint 调用）。"""
    with session() as conn:
        return recompute(conn)


def _fmt_ts(t: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
