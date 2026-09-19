"""评估引擎：给定估值日，输出可逐日复核、可版本化的条款判断。

核心语义：
- 滚动窗口只计入"适用交易日"：交易日历内的交易日，且正股未停牌、存在有效
  官方收盘价（official_close / corrected）。停牌、缺价、仅有盘中临时价、
  价格被撤回的日子被分类排除并逐日列入证据；
- 每个被计入的日子用"当日有效转股价 × 阈值比例"比较，比较过程逐日留痕；
- 每次评估生成新的判断版本（version 递增），旧版本完整保留；
- 引用过旧版本的外部结论在新版本产生时被圈出（stale），并附差异摘要。
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .calendar import TradingCalendar
from .dependencies import evaluation_order
from .models import EVENT_ACTIVE, Bond, Event, PriceBar
from .pricing import build_price_timeline, price_on
from .store import Store
from .util import canonical_json, dec, dec_str, iso, money_str, parse_date, utcnow_iso

_ONE_DAY = timedelta(days=1)

# 条款状态
STATE_NOT_APPLICABLE = "not_applicable"  # 尚未进入适用期
STATE_EXPIRED = "expired"  # 适用期已过
STATE_ACCUMULATING = "accumulating"  # 观察期重起后适用日尚不足一个完整窗口
STATE_COUNTING = "counting"  # 窗口完整，未达标
STATE_CONDITION_MET = "condition_met"  # 触发条件已满足
STATE_ACTIVE = "active"  # 到期条款：存续中
STATE_MATURED = "matured"

# 日内分类
DAY_COUNTED = "counted"
DAY_SUSPENDED = "suspended"
DAY_MISSING_PRICE = "missing_price"
DAY_TEMPORARY_ONLY = "temporary_only"
DAY_RETRACTED_PRICE = "retracted_price"

# 属于"数据缺口"的分类（停牌是已知市场状态，不算数据缺口）
GAP_STATUSES = (DAY_MISSING_PRICE, DAY_TEMPORARY_ONLY, DAY_RETRACTED_PRICE)

COMPARISONS = (">=", ">", "<=", "<")

_SCAN_CAP_DAYS = 400  # 窗口回溯扫描上限（自然日）
_FUTURE_SCAN_CAP = 90  # 未来达标日期推演上限（交易日）


class EvaluationError(ValueError):
    """评估请求本身有问题（参数非法、状态不允许）。"""


class NotFoundError(EvaluationError):
    """引用的对象不存在。"""


# ---------------------------------------------------------------- 行情索引与日内分类


def index_prices(bars: list[PriceBar]) -> dict[date, dict[str, PriceBar]]:
    """每个日期取 active 的官方价与临时价各一条（修订取最新 revision）。"""
    index: dict[date, dict[str, PriceBar]] = {}
    for bar in bars:
        if bar.status != "active":
            continue
        slot = "temporary" if bar.source == "temporary" else "official"
        current = index.setdefault(bar.bar_date, {})
        if slot not in current or bar.revision > current[slot].revision:
            current[slot] = bar
    return index


def classify_day(
    d: date,
    price_index: dict[date, dict[str, PriceBar]],
    suspensions: list[Event],
) -> tuple[str, PriceBar | None, str]:
    for susp in suspensions:
        start = parse_date(susp.params["start_date"])
        end = parse_date(susp.params["end_date"])
        if start <= d <= end:
            return DAY_SUSPENDED, None, f"正股停牌: {susp.params.get('reason', susp.event_id)}"
    entry = price_index.get(d)
    if not entry:
        return DAY_MISSING_PRICE, None, "无行情记录"
    if "official" in entry:
        return DAY_COUNTED, entry["official"], ""
    if "temporary" in entry:
        return DAY_TEMPORARY_ONLY, None, "仅有盘中临时价，正式判断不予采用"
    return DAY_RETRACTED_PRICE, None, "行情已被撤回"


def _active_suspensions(events: list[Event]) -> list[Event]:
    return [e for e in events if e.event_type == "suspension" and e.status == EVENT_ACTIVE]


# ---------------------------------------------------------------- 窗口收集


def _compare(close: Decimal, comparison: str, threshold: Decimal) -> bool:
    if comparison == ">=":
        return close >= threshold
    if comparison == ">":
        return close > threshold
    if comparison == "<=":
        return close <= threshold
    if comparison == "<":
        return close < threshold
    raise EvaluationError(f"未知比较符: {comparison}")


def collect_window(
    clause: dict[str, Any],
    window_end: date,
    calendar: TradingCalendar,
    price_index: dict[date, dict[str, PriceBar]],
    suspensions: list[Event],
    timeline: list[dict[str, Any]],
    bond: Bond,
    not_before: date | None,
) -> tuple[list[dict], list[dict]]:
    """从 window_end 向前收集适用交易日，直到凑满 window_days 或触达边界。"""
    window_days = int(clause["window_days"])
    ratio = dec(clause["threshold_ratio"])
    comparison = clause.get("comparison", ">=")
    if comparison not in COMPARISONS:
        raise EvaluationError(f"条款 {clause.get('clause_id')} 比较符非法: {comparison}")

    counted: list[dict] = []
    excluded: list[dict] = []
    d = window_end
    scanned = 0
    while len(counted) < window_days and scanned < _SCAN_CAP_DAYS:
        scanned += 1
        if not calendar.is_trading_day(d):
            if d < calendar.start:
                break
            d -= _ONE_DAY
            continue
        if d < bond.list_date or (not_before and d < not_before):
            break
        status, bar, reason = classify_day(d, price_index, suspensions)
        if status == DAY_COUNTED and bar is not None:
            conv_price = price_on(timeline, d)
            threshold = conv_price * ratio
            hit = _compare(bar.close, comparison, threshold)
            counted.append(
                {
                    "date": iso(d),
                    "close": dec_str(bar.close),
                    "conversion_price": dec_str(conv_price),
                    "threshold": money_str(threshold),
                    "comparison": comparison,
                    "hit": hit,
                    "margin": money_str(bar.close - threshold),
                    "price_source": bar.source,
                    "price_revision": bar.revision,
                }
            )
        else:
            entry: dict[str, Any] = {"date": iso(d), "status": status, "reason": reason}
            temp = price_index.get(d, {}).get("temporary")
            if temp is not None:
                entry["temporary_close"] = dec_str(temp.close)
            excluded.append(entry)
        d -= _ONE_DAY

    counted.reverse()
    excluded.reverse()
    return counted, excluded


def _future_hit_dates(
    calendar: TradingCalendar,
    suspensions: list[Event],
    after: date,
    count: int,
) -> list[str]:
    """假设未来每个适用交易日都达标，列出接下来 count 个适用交易日。"""
    dates: list[str] = []
    d = after
    scanned = 0
    while len(dates) < count and scanned < _FUTURE_SCAN_CAP:
        nxt = calendar.next_trading_day(d)
        if nxt is None:
            break
        scanned += 1
        status, _, _ = classify_day(nxt, {}, suspensions)
        if status != DAY_SUSPENDED:
            dates.append(iso(nxt))
        d = nxt
    return dates


# ---------------------------------------------------------------- 条款求值


def _clause_period(clause: dict[str, Any], bond: Bond) -> tuple[date, date]:
    start = parse_date(clause["applies_from"]) if clause.get("applies_from") else bond.list_date
    end = (
        parse_date(clause["applies_until"]) if clause.get("applies_until") else bond.maturity_date
    )
    return start, end


def _window_reset_date(
    clause: dict[str, Any], events: list[Event], clauses_by_id: dict[str, dict]
) -> date | None:
    """观察期重起点：window_reset_on 引用的下修类条款对应事件的最后生效日。"""
    refs = clause.get("window_reset_on")
    if not refs:
        return None
    reset_clause_ids = {
        cid for cid, c in clauses_by_id.items() if c.get("type") == "conversion_price_reset"
    }
    wants_any_reset = any(
        ref in reset_clause_ids or ref == "conversion_price_reset" for ref in refs
    )
    if not wants_any_reset:
        return None
    effective = [
        e.effective_date
        for e in events
        if e.event_type == "reset" and e.status == EVENT_ACTIVE and e.effective_date
    ]
    return max(effective) if effective else None


def evaluate_window_clause(
    clause: dict[str, Any],
    valuation_date: date,
    bond: Bond,
    calendar: TradingCalendar,
    price_index: dict[date, dict[str, PriceBar]],
    events: list[Event],
    timeline: list[dict[str, Any]],
    clauses_by_id: dict[str, dict],
) -> dict[str, Any]:
    cid = clause["clause_id"]
    result: dict[str, Any] = {
        "clause_id": cid,
        "type": clause["type"],
        "comparison": clause.get("comparison", ">="),
        "threshold_ratio": clause.get("threshold_ratio"),
    }
    period_start, period_end = _clause_period(clause, bond)
    result["applies_from"] = iso(period_start)
    result["applies_until"] = iso(period_end)
    if valuation_date > period_end:
        result["state"] = STATE_EXPIRED
        return result
    if valuation_date < period_start:
        result["state"] = STATE_NOT_APPLICABLE
        result["reason"] = f"适用期自 {iso(period_start)} 起"
        return result

    window_end = calendar.floor(valuation_date)
    if window_end is None or window_end < bond.list_date:
        result["state"] = STATE_NOT_APPLICABLE
        result["reason"] = "估值日之前无可用交易日"
        return result
    if window_end != valuation_date:
        result["as_of_date"] = iso(window_end)

    suspensions = _active_suspensions(events)
    reset_after = _window_reset_date(clause, events, clauses_by_id)
    counted, excluded = collect_window(
        clause, window_end, calendar, price_index, suspensions, timeline, bond, reset_after
    )

    window_days = int(clause["window_days"])
    required_hits = int(clause["required_hits"])
    hits = sum(1 for day in counted if day["hit"])
    hits_needed = max(0, required_hits - hits)

    if hits >= required_hits:
        state = STATE_CONDITION_MET
    elif len(counted) < window_days:
        state = STATE_ACCUMULATING
    else:
        state = STATE_COUNTING

    needed_dates = _future_hit_dates(calendar, suspensions, window_end, hits_needed)
    result.update(
        {
            "state": state,
            "window": {
                "window_days": window_days,
                "required_hits": required_hits,
                "counted_days": len(counted),
                "hits": hits,
                "window_start": counted[0]["date"] if counted else None,
                "window_end": iso(window_end),
                "window_reset_after": iso(reset_after) if reset_after else None,
            },
            "remaining": {
                "hits_needed": hits_needed,
                "needed_dates": needed_dates,
                "earliest_trigger_date": needed_dates[-1] if needed_dates else None,
                "assumption": "假设未来每个适用交易日均达标",
            },
            "days": counted,
            "excluded_days": excluded,
            "gaps": [d for d in excluded if d["status"] in GAP_STATUSES],
        }
    )
    return result


def evaluate_maturity_clause(
    clause: dict[str, Any],
    valuation_date: date,
    bond: Bond,
    calendar: TradingCalendar,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "clause_id": clause["clause_id"],
        "type": "maturity",
        "maturity_date": iso(bond.maturity_date),
    }
    if valuation_date > bond.maturity_date:
        result["state"] = STATE_MATURED
        return result
    result["state"] = STATE_ACTIVE
    result["calendar_days_to_maturity"] = (bond.maturity_date - valuation_date).days
    floor_date = calendar.floor(valuation_date)
    if floor_date is not None:
        result["trading_days_to_maturity"] = calendar.count_trading_days_between(
            floor_date, bond.maturity_date
        )
    return result


# ---------------------------------------------------------------- 差异与引用


def diff_judgments(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """生成两个判断版本之间的可读差异摘要。"""
    diffs: list[str] = []
    if old.get("effective_conversion_price") != new.get("effective_conversion_price"):
        diffs.append(
            f"转股价 {old.get('effective_conversion_price')} → {new.get('effective_conversion_price')}"
        )
    old_clauses = {c["clause_id"]: c for c in old.get("clauses", [])}
    for clause in new.get("clauses", []):
        prev = old_clauses.get(clause["clause_id"])
        if prev is None:
            continue
        changes: list[str] = []
        if prev.get("state") != clause.get("state"):
            changes.append(f"状态 {prev.get('state')} → {clause.get('state')}")
        prev_window = prev.get("window") or {}
        new_window = clause.get("window") or {}
        if prev_window.get("hits") != new_window.get("hits"):
            changes.append(
                f"达标天数 {prev_window.get('hits')} → {new_window.get('hits')}"
                f"（要求 {new_window.get('required_hits')}）"
            )
        if prev_window.get("counted_days") != new_window.get("counted_days"):
            changes.append(
                f"计入交易日 {prev_window.get('counted_days')} → {new_window.get('counted_days')}"
            )
        if changes:
            diffs.append(f"{clause['clause_id']}: " + "；".join(changes))
    old_gaps = {g["date"] for c in old.get("clauses", []) for g in c.get("gaps", [])}
    new_gaps = {g["date"] for c in new.get("clauses", []) for g in c.get("gaps", [])}
    if old_gaps != new_gaps:
        resolved = sorted(old_gaps - new_gaps)
        appeared = sorted(new_gaps - old_gaps)
        if resolved:
            diffs.append("数据缺口补齐: " + ", ".join(resolved))
        if appeared:
            diffs.append("新增数据缺口: " + ", ".join(appeared))
    return diffs


def add_reference(
    store: Store,
    judgment_id: str,
    consumer: str,
    note: str = "",
    now: str | None = None,
) -> dict[str, Any]:
    judgment = store.get_judgment(judgment_id)
    if judgment is None:
        raise NotFoundError(f"判断版本不存在: {judgment_id}")
    reference = {
        "reference_id": f"R{len(store.list_references()) + 1:04d}",
        "judgment_id": judgment_id,
        "bond_id": judgment["bond_id"],
        "valuation_date": judgment["valuation_date"],
        "consumer": consumer,
        "note": note,
        "created_at": now or utcnow_iso(),
        "status": "current",
        "superseded_by": None,
        "diff": [],
    }
    store.add_reference(reference)
    return reference


def _mark_references_stale(store: Store, new_judgment: dict[str, Any]) -> list[dict[str, Any]]:
    """把引用了同一 (债券, 估值日) 旧版本的外部结论圈出，附差异，不改动旧版本本身。"""
    flagged: list[dict[str, Any]] = []
    for ref in store.list_references(status="current"):
        if ref["bond_id"] != new_judgment["bond_id"]:
            continue
        if ref["valuation_date"] != new_judgment["valuation_date"]:
            continue
        if ref["judgment_id"] == new_judgment["judgment_id"]:
            continue
        old = store.get_judgment(ref["judgment_id"])
        diff = diff_judgments(old, new_judgment) if old else []
        store.update_reference(
            ref["reference_id"],
            status="stale",
            superseded_by=new_judgment["judgment_id"],
            diff=diff,
        )
        flagged.append({**ref, "status": "stale", "superseded_by": new_judgment["judgment_id"], "diff": diff})
    return flagged


# ---------------------------------------------------------------- 主入口


def evaluate(
    store: Store,
    bond_id: str,
    valuation_date: str | date,
    note: str = "",
    now: str | None = None,
) -> dict[str, Any]:
    bond = store.get_bond(bond_id)
    if bond is None:
        raise NotFoundError(f"债券不存在: {bond_id}")
    calendar = store.get_calendar(bond.calendar_id)
    if calendar is None:
        raise NotFoundError(f"交易日历不存在: {bond.calendar_id}")
    vdate = parse_date(valuation_date)
    if vdate < bond.list_date:
        raise EvaluationError(f"估值日 {iso(vdate)} 早于上市日 {iso(bond.list_date)}")

    order = evaluation_order(bond.clauses)  # 循环依赖在此被阻止
    clauses_by_id = {c["clause_id"]: c for c in bond.clauses}
    events = store.get_events(bond_id)
    timeline = build_price_timeline(bond, events)
    price_index = index_prices(store.get_prices(bond.stock_code))

    clause_results: list[dict[str, Any]] = []
    for cid in order:
        clause = clauses_by_id[cid]
        if clause.get("type") == "maturity":
            clause_results.append(evaluate_maturity_clause(clause, vdate, bond, calendar))
        else:
            clause_results.append(
                evaluate_window_clause(
                    clause, vdate, bond, calendar, price_index, events, timeline, clauses_by_id
                )
            )

    gaps: dict[str, dict] = {}
    for result in clause_results:
        for gap in result.get("gaps", []):
            gaps[gap["date"]] = gap

    judgment: dict[str, Any] = {
        "bond_id": bond_id,
        "valuation_date": iso(vdate),
        "data_version": store.data_version,
        "created_at": now or utcnow_iso(),
        "note": note,
        "effective_conversion_price": dec_str(price_on(timeline, vdate)),
        "conversion_price_timeline": timeline,
        "dependency_order": order,
        "clauses": clause_results,
        "gaps": [gaps[k] for k in sorted(gaps)],
        "retracted_events": [e.to_dict() for e in events if e.status == "retracted"],
    }
    judgment["evidence_hash"] = hashlib.sha256(canonical_json(judgment).encode("utf-8")).hexdigest()

    previous = store.latest_judgment(bond_id, iso(vdate))
    version = (previous["version"] + 1) if previous else 1
    judgment["version"] = version
    judgment["judgment_id"] = f"J{bond_id}-{vdate.strftime('%Y%m%d')}-v{version}"
    judgment["supersedes"] = previous["judgment_id"] if previous else None
    judgment["changes_from_previous"] = diff_judgments(previous, judgment) if previous else []

    store.save_judgment(judgment)
    flagged = _mark_references_stale(store, judgment)
    judgment["stale_references"] = flagged
    return judgment


def scan_gaps(store: Store, bond_id: str, valuation_date: str | date) -> dict[str, Any]:
    """数据缺口扫描：从估值日向前回溯，列出所有非适用交易日及其分类。"""
    bond = store.get_bond(bond_id)
    if bond is None:
        raise NotFoundError(f"债券不存在: {bond_id}")
    calendar = store.get_calendar(bond.calendar_id)
    if calendar is None:
        raise NotFoundError(f"交易日历不存在: {bond.calendar_id}")
    vdate = parse_date(valuation_date)
    events = store.get_events(bond_id)
    suspensions = _active_suspensions(events)
    price_index = index_prices(store.get_prices(bond.stock_code))

    max_window = max(
        (int(c.get("window_days", 0)) for c in bond.clauses if c.get("type") != "maturity"),
        default=30,
    )
    target_counted = max_window + 15  # 多扫一段，覆盖窗口重起等情形
    counted = 0
    excluded: list[dict] = []
    d = calendar.floor(vdate)
    scanned = 0
    while d is not None and counted < target_counted and scanned < _SCAN_CAP_DAYS:
        scanned += 1
        if d < bond.list_date:
            break
        status, _, reason = classify_day(d, price_index, suspensions)
        if status == DAY_COUNTED:
            counted += 1
        else:
            entry: dict[str, Any] = {"date": iso(d), "status": status, "reason": reason}
            temp = price_index.get(d, {}).get("temporary")
            if temp is not None:
                entry["temporary_close"] = dec_str(temp.close)
            excluded.append(entry)
        d = calendar.prev_trading_day(d)

    excluded.reverse()
    return {
        "bond_id": bond_id,
        "date": iso(vdate),
        "scanned_counted_days": counted,
        "excluded_days": excluded,
        "gaps": [e for e in excluded if e["status"] in GAP_STATUSES],
        "suspensions": [e for e in excluded if e["status"] == DAY_SUSPENDED],
    }
