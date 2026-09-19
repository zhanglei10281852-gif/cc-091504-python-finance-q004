"""条款归一化、依赖排序与观察窗口评估。

窗口只计入"适用交易日"：交易日历覆盖、条款观察期内、未停牌且有正式
收盘价（official_close / corrected）的日子。停牌、缺价、仅有盘中临时价
的日子分别归类为 excluded，不进窗口、不中断连续性，但全部留在证据里。

条款间的相互影响（观察期被其他条款的触发截断、显式依赖）通过依赖图
给出确定性的评估顺序：Kahn 拓扑排序 + 条款ID字典序破平；出现循环依赖
直接拒绝该条款版本，不允许进入评估。
"""
from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .calendar import TradingCalendar
from .models import (
    CLAUSE_TYPES,
    ValidationError,
    compare,
    money_str,
    parse_date,
    parse_money,
    q4,
    ratio_str,
    require_choice,
    require_fields,
)
from .price_schedule import PriceSchedule

WINDOW_KINDS = ("m_of_n", "all_of_n")
OPS = (">=", "<=", ">", "<")


class ClauseDependencyError(ValueError):
    """条款依赖存在循环或冲突，该条款版本不可评估。"""


@dataclass
class Period:
    start: date
    end: date
    end_reason: str


# ---------- 条款归一化 ----------


def normalize_clause(raw: dict) -> dict:
    require_fields(raw, ["clause_id", "type"], "clause")
    clause_id = str(raw["clause_id"])
    clause_type = require_choice(raw["type"], CLAUSE_TYPES, f"clause[{clause_id}].type")
    clause = {"clause_id": clause_id, "type": clause_type}

    if clause_type == "maturity":
        return clause  # 到期条款是日期事实，无窗口

    require_fields(raw, ["window", "condition"], f"clause[{clause_id}]")
    window = raw["window"]
    kind = require_choice(window.get("kind"), WINDOW_KINDS, f"clause[{clause_id}].window.kind")
    n = window.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValidationError(f"clause[{clause_id}].window.n: 需要正整数")
    m = window.get("m")
    if kind == "m_of_n":
        if not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= n:
            raise ValidationError(f"clause[{clause_id}].window.m: 需要 1<=m<=n 的整数")
    elif m is not None:
        raise ValidationError(f"clause[{clause_id}].window: all_of_n 不接受 m")
    clause["window"] = {"kind": kind, "n": n, **({"m": m} if kind == "m_of_n" else {})}

    condition = raw["condition"]
    op = require_choice(condition.get("op"), OPS, f"clause[{clause_id}].condition.op")
    pct = parse_money(condition.get("pct"), f"clause[{clause_id}].condition.pct")
    if pct <= 0:
        raise ValidationError(f"clause[{clause_id}].condition.pct: 必须为正")
    clause["condition"] = {"op": op, "pct": str(pct)}

    effective = raw.get("effective", {})
    clause["effective"] = {
        "from": effective.get("from", "conversion_period_start"),
        "to": effective.get("to", "maturity"),
    }
    observe_until = raw.get("observe_until")
    if observe_until:
        trigger_of = observe_until.get("trigger_of")
        if not trigger_of:
            raise ValidationError(f"clause[{clause_id}].observe_until: 需要 trigger_of")
        clause["observe_until"] = {"trigger_of": str(trigger_of)}
    depends_on = raw.get("depends_on", [])
    if not isinstance(depends_on, list):
        raise ValidationError(f"clause[{clause_id}].depends_on: 需要数组")
    clause["depends_on"] = [str(d) for d in depends_on]
    return clause


def normalize_terms(payload: dict) -> dict:
    require_fields(payload, [
        "bond_id", "name", "underlying", "issue_date", "maturity_date",
        "conversion_period", "initial_conversion_price", "clauses",
    ], "terms")
    for f in ("issue_date", "maturity_date"):
        parse_date(payload[f], f"terms.{f}")
    cp = payload["conversion_period"]
    require_fields(cp, ["start", "end"], "terms.conversion_period")
    parse_date(cp["start"], "terms.conversion_period.start")
    parse_date(cp["end"], "terms.conversion_period.end")
    price = parse_money(payload["initial_conversion_price"], "terms.initial_conversion_price")
    if price <= 0:
        raise ValidationError("terms.initial_conversion_price: 必须为正")
    if not isinstance(payload["clauses"], list) or not payload["clauses"]:
        raise ValidationError("terms.clauses: 需要非空数组")

    clauses = [normalize_clause(c) for c in payload["clauses"]]
    ids = [c["clause_id"] for c in clauses]
    if len(set(ids)) != len(ids):
        raise ValidationError(f"terms.clauses: clause_id 重复 {ids}")
    # 依赖校验 + 排序在入库前完成，循环依赖直接拒绝
    dependency_order(clauses)

    normalized = {
        "bond_id": str(payload["bond_id"]),
        "name": str(payload["name"]),
        "underlying": str(payload["underlying"]),
        "issue_date": payload["issue_date"],
        "maturity_date": payload["maturity_date"],
        "maturity_redemption_price": str(parse_money(
            payload.get("maturity_redemption_price", "0"), "terms.maturity_redemption_price")),
        "conversion_period": {"start": cp["start"], "end": cp["end"]},
        "initial_conversion_price": str(price),
        "clauses": clauses,
    }
    if payload.get("calendar_id"):
        normalized["calendar_id"] = str(payload["calendar_id"])
    # 观察期锚点在入库前解析一遍，非法配置尽早暴露
    for clause in clauses:
        if clause["type"] == "maturity":
            continue
        resolve_period(clause, normalized, {})
    return normalized


# ---------- 依赖排序 ----------


def _dependency_edges(clauses: list[dict]) -> dict[str, set[str]]:
    """返回 {clause_id: 其评估依赖的 clause_id 集合}。"""
    ids = {c["clause_id"] for c in clauses}
    edges: dict[str, set[str]] = {c["clause_id"]: set() for c in clauses}
    for c in clauses:
        cid = c["clause_id"]
        refs: list[str] = []
        if c.get("observe_until"):
            refs.append(c["observe_until"]["trigger_of"])
        for dep in c.get("depends_on", []):
            if dep.startswith("trigger:"):
                refs.append(dep[len("trigger:"):])
            elif dep == "conversion_price":
                continue  # 转股价时间线在条款评估前已由记录构建，恒先序
            else:
                raise ValidationError(f"clause[{cid}].depends_on: 未知依赖 {dep!r}")
        for ref in refs:
            if ref == cid:
                raise ClauseDependencyError(f"条款 {cid} 依赖自身")
            if ref not in ids:
                raise ValidationError(f"clause[{cid}]: 引用了不存在的条款 {ref!r}")
            edges[cid].add(ref)
    return edges


def dependency_order(clauses: list[dict]) -> list[str]:
    """确定性拓扑序：Kahn 算法，可用节点按 clause_id 字典序破平。"""
    edges = _dependency_edges(clauses)
    indegree = {cid: len(deps) for cid, deps in edges.items()}
    dependents: dict[str, list[str]] = {cid: [] for cid in edges}
    for cid, deps in edges.items():
        for dep in deps:
            dependents[dep].append(cid)

    heap = [cid for cid, deg in indegree.items() if deg == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        cid = heapq.heappop(heap)
        order.append(cid)
        for nxt in dependents[cid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(heap, nxt)
    if len(order) != len(clauses):
        cycle = sorted(cid for cid, deg in indegree.items() if deg > 0)
        raise ClauseDependencyError(
            f"条款依赖存在循环，无法确定评估顺序: {' -> '.join(cycle)}")
    return order


# ---------- 观察期 ----------


def _resolve_anchor(anchor: str, terms: dict) -> date:
    if anchor == "issue":
        return parse_date(terms["issue_date"], "issue_date")
    if anchor == "conversion_period_start":
        return parse_date(terms["conversion_period"]["start"], "conversion_period.start")
    if anchor == "conversion_period_end":
        return parse_date(terms["conversion_period"]["end"], "conversion_period.end")
    if anchor == "maturity":
        return parse_date(terms["maturity_date"], "maturity_date")
    if anchor.startswith("maturity_minus_years:"):
        try:
            years = int(anchor.split(":", 1)[1])
        except ValueError as exc:
            raise ValidationError(f"effective anchor: 非法的年数锚点 {anchor!r}") from exc
        maturity = parse_date(terms["maturity_date"], "maturity_date")
        try:
            return maturity.replace(year=maturity.year - years)
        except ValueError:  # 2月29日
            return maturity.replace(year=maturity.year - years, day=28)
    return parse_date(anchor, "effective anchor")


def resolve_period(clause: dict, terms: dict, trigger_dates: dict[str, date]) -> Period:
    start = _resolve_anchor(clause["effective"]["from"], terms)
    end = _resolve_anchor(clause["effective"]["to"], terms)
    maturity = parse_date(terms["maturity_date"], "maturity_date")
    end_reason = "clause"
    if end > maturity:
        end, end_reason = maturity, "maturity"
    if clause.get("observe_until"):
        other = clause["observe_until"]["trigger_of"]
        if other in trigger_dates and trigger_dates[other] < end:
            end, end_reason = trigger_dates[other], f"trigger_of:{other}"
    return Period(start=start, end=end, end_reason=end_reason)


# ---------- 窗口评估 ----------


def evaluate_window_clause(
    clause: dict,
    *,
    calendar: TradingCalendar,
    official: dict[date, dict],
    temporary: dict[date, list[dict]],
    suspensions: dict[date, dict],
    schedule: PriceSchedule,
    terms: dict,
    valuation_date: date,
    trigger_dates: dict[str, date],
    used_quotes: dict[str, dict],
) -> dict:
    cid = clause["clause_id"]
    period = resolve_period(clause, terms, trigger_dates)
    base = {
        "clause_id": cid,
        "type": clause["type"],
        "window": clause["window"],
        "condition": clause["condition"],
        "period": {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "end_reason": period.end_reason,
        },
    }
    if valuation_date < period.start:
        return {**base, "status": "not_started",
                "note": "估值日早于观察期起点", "days": [], "excluded": [],
                "data_gaps": [], "summary": None, "trigger": None, "projection": None}

    end = min(period.end, valuation_date)
    pct = parse_money(clause["condition"]["pct"], "condition.pct")
    op = clause["condition"]["op"]

    days: list[dict] = []
    excluded: list[dict] = []
    for d in calendar.days_between(period.start, end):
        key = d.isoformat()
        if d in suspensions:
            rec = suspensions[d]
            excluded.append({"date": key, "reason": "suspended",
                             "detail": rec.get("reason") or "停牌",
                             "record_id": rec["record_id"]})
            continue
        rec = official.get(d)
        if rec is None:
            temps = temporary.get(d)
            if temps:
                for t in temps:
                    used_quotes.setdefault(key, {})["temporary"] = \
                        [r["record_id"] for r in temps]
                excluded.append({"date": key, "reason": "temporary_only",
                                 "detail": "仅有盘中临时价，未计入窗口",
                                 "record_id": temps[-1]["record_id"]})
            else:
                excluded.append({"date": key, "reason": "missing_price",
                                 "detail": "交易日缺正式收盘价"})
            continue
        used_quotes.setdefault(key, {})["official"] = rec["record_id"]
        conv_price = schedule.price_at(d)
        close = parse_money(rec["close"], "quote.close")
        threshold = q4(conv_price * pct)
        hit = compare(op, close, threshold)
        days.append({
            "date": key,
            "close": money_str(close),
            "conversion_price": money_str(conv_price),
            "threshold": str(threshold),
            "compare": f"{money_str(close)} {op} {threshold}",
            "ratio": ratio_str(close / conv_price),
            "hit": hit,
            "record_id": rec["record_id"],
            "record_version": rec["record_version"],
            "price_source": rec["price_source"],
        })

    hits_seq = [bool(d["hit"]) for d in days]
    n = clause["window"]["n"]
    kind = clause["window"]["kind"]
    m = clause["window"].get("m")

    trigger = None
    if kind == "m_of_n":
        run = 0
        for i, h in enumerate(hits_seq):
            run += 1 if h else 0
            if i >= n and hits_seq[i - n]:
                run -= 1
            if run >= m:
                trigger = {"triggered": True, "trigger_date": days[i]["date"],
                           "window_start": days[max(0, i - n + 1)]["date"],
                           "hits_in_window": run}
                break
    else:  # all_of_n
        streak = 0
        for i, h in enumerate(hits_seq):
            streak = streak + 1 if h else 0
            if streak >= n:
                trigger = {"triggered": True, "trigger_date": days[i]["date"],
                           "window_start": days[i - n + 1]["date"],
                           "hits_in_window": n}
                break

    # 当前窗口统计
    tail = hits_seq[-n:]
    if kind == "m_of_n":
        hits_now = sum(tail)
        needed = max(0, m - hits_now)
        current_streak = None
    else:
        current_streak = 0
        for h in reversed(hits_seq):
            if not h:
                break
            current_streak += 1
        hits_now = current_streak
        needed = max(0, n - current_streak)

    summary = {
        "applicable_days": len(days),
        "hits_total": sum(hits_seq),
        "current_window_size": len(tail),
        "hits_in_current_window": hits_now,
        "needed_to_trigger": needed,
    }
    if current_streak is not None:
        summary["current_streak"] = current_streak

    # 触发距离推演：假设未来每个适用交易日都达标，求最早触发日
    projection = None
    if trigger is None and valuation_date <= period.end:
        horizon = max(2 * n, needed + 1)
        future = calendar.next_days(valuation_date, horizon, end=period.end)
        sim = deque(tail, maxlen=n)
        earliest = None
        for idx, f in enumerate(future):
            if kind == "m_of_n":
                sim.append(True)
                if sum(sim) >= m:
                    earliest = idx
                    break
            else:
                if idx + 1 >= needed:
                    earliest = needed - 1
                    break
        if earliest is not None:
            projection = {
                "assumption": "假设未来每个适用交易日均达标",
                "earliest_trigger_date": future[earliest].isoformat(),
                "must_hit_dates": [f.isoformat() for f in future[:earliest + 1]],
                "reachable": True,
            }
        else:
            projection = {
                "assumption": "假设未来每个适用交易日均达标",
                "earliest_trigger_date": None,
                "must_hit_dates": [f.isoformat() for f in future],
                "reachable": False,
                "note": "观察期或日历覆盖内无法达成",
            }

    gaps = [e for e in excluded if e["reason"] in ("missing_price", "temporary_only")]
    if calendar.last_day is not None and end > calendar.last_day:
        gaps.append({"date": end.isoformat(), "reason": "calendar_not_covered",
                     "detail": f"交易日历仅覆盖至 {calendar.last_day.isoformat()}"})

    if trigger:
        status = "triggered"
    elif valuation_date > period.end:
        status = "ended_no_trigger"
    else:
        status = "monitoring"

    return {
        **base,
        "status": status,
        "days": days,
        "excluded": excluded,
        "data_gaps": gaps,
        "summary": summary,
        "trigger": trigger or {"triggered": False, "trigger_date": None},
        "projection": projection,
    }


def evaluate_maturity_clause(clause: dict, *, terms: dict, valuation_date: date) -> dict:
    maturity = parse_date(terms["maturity_date"], "maturity_date")
    matured = valuation_date >= maturity
    return {
        "clause_id": clause["clause_id"],
        "type": "maturity",
        "status": "matured" if matured else "active",
        "maturity_date": maturity.isoformat(),
        "days_to_maturity": (maturity - valuation_date).days,
        "maturity_redemption_price": terms["maturity_redemption_price"],
        "conversion_period_end": terms["conversion_period"]["end"],
    }
