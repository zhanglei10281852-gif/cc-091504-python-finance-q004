"""条款依赖排序：把"谁改变转股价/观察期、谁读取它们"显式建模为 DAG。

规则：
- 窗口类条款（强赎/回售/下修触发）默认读取 conversion_price；
- 声明了 modifies 的条款会向所有读取这些目标的条款连边；
- depends_on / window_reset_on 可显式引用其他 clause_id（window_reset_on 也接受
  条款类型名，如 conversion_price_reset）；
- 拓扑排序按 (类型优先级, clause_id) 确定性打破平局，保证同一输入永远得到同一顺序；
- 出现环（含自环、未知引用）时拒绝求值并给出环路径。
"""

from __future__ import annotations

from typing import Any

WINDOW_CLAUSE_TYPES = ("redemption", "put", "conversion_price_reset")
TYPE_PRIORITY = {
    "conversion_price_reset": 0,
    "redemption": 1,
    "put": 2,
    "maturity": 3,
}


class ClauseDependencyError(ValueError):
    def __init__(self, message: str, cycle: list[str] | None = None) -> None:
        super().__init__(message)
        self.cycle = cycle or []


def clause_reads(clause: dict[str, Any]) -> set[str]:
    reads = set(clause.get("reads", []))
    if clause.get("type") in WINDOW_CLAUSE_TYPES:
        reads.add("conversion_price")
    return reads


def _resolve_refs(clauses_by_id: dict[str, dict], refs: list[str], owner: str) -> list[str]:
    """把 window_reset_on / depends_on 中的引用解析为 clause_id 列表。"""
    resolved: list[str] = []
    known_types = {c.get("type") for c in clauses_by_id.values()}
    for ref in refs:
        if ref in clauses_by_id:
            resolved.append(ref)
        elif ref in known_types:
            resolved.extend(c["clause_id"] for c in clauses_by_id.values() if c.get("type") == ref)
        else:
            raise ClauseDependencyError(f"条款 {owner} 引用了不存在的条款或类型: {ref}")
    return resolved


def build_dependency_edges(clauses: list[dict[str, Any]]) -> dict[str, set[str]]:
    """返回依赖边：{被依赖者 -> {依赖者}}。"""
    clauses_by_id: dict[str, dict] = {}
    for clause in clauses:
        cid = clause.get("clause_id")
        if not cid:
            raise ClauseDependencyError("存在缺少 clause_id 的条款")
        if cid in clauses_by_id:
            raise ClauseDependencyError(f"clause_id 重复: {cid}")
        clauses_by_id[cid] = clause

    edges: dict[str, set[str]] = {cid: set() for cid in clauses_by_id}
    for clause in clauses:
        cid = clause["clause_id"]
        explicit = list(clause.get("depends_on", [])) + list(clause.get("window_reset_on", []))
        for dep in _resolve_refs(clauses_by_id, explicit, cid):
            if dep == cid:
                raise ClauseDependencyError(f"条款 {cid} 依赖自身", cycle=[cid, cid])
            edges[dep].add(cid)
        reads = clause_reads(clause)
        for other in clauses:
            other_id = other["clause_id"]
            if other_id == cid:
                continue
            if reads & set(other.get("modifies", [])):
                edges[other_id].add(cid)
    return edges


def _find_cycle(edges: dict[str, set[str]]) -> list[str]:
    """在残余图中找一条环路径用于报错（边方向：被依赖者 -> 依赖者）。"""
    state: dict[str, int] = {}
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            if state.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if state.get(nxt, 0) == 0:
                found = dfs(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(edges):
        if state.get(node, 0) == 0:
            found = dfs(node)
            if found:
                return found
    return []


def evaluation_order(clauses: list[dict[str, Any]]) -> list[str]:
    """返回确定性的条款求值顺序；存在循环依赖时抛出 ClauseDependencyError。"""
    edges = build_dependency_edges(clauses)
    indegree = {cid: 0 for cid in edges}
    for dependents in edges.values():
        for dep in dependents:
            indegree[dep] += 1

    def sort_key(cid: str) -> tuple[int, str]:
        clause = next(c for c in clauses if c["clause_id"] == cid)
        return (TYPE_PRIORITY.get(clause.get("type"), 99), cid)

    ready = sorted((cid for cid, deg in indegree.items() if deg == 0), key=sort_key)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for dependent in sorted(edges[node], key=sort_key):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
        ready.sort(key=sort_key)

    if len(order) != len(edges):
        cycle = _find_cycle(edges)
        raise ClauseDependencyError(
            "条款存在循环依赖，已阻止求值: " + " -> ".join(cycle), cycle=cycle
        )
    return order
