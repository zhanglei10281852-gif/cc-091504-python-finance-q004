"""评估编排：汇集某一 ingest 时点的全部输入，产出带证据的判断内容。

判断内容包含：
- dependency_order：条款评估的确定性顺序；
- effective_terms：估值日当前有效条款（转股价时间线、生效条款）；
- clauses：每个条款逐日证据（适用日如何比较、被排除日的原因、数据缺口）；
- manifest：输入清单（各实体版本 + as_of_seq），历史判断可据此精确回放。

content_hash 只覆盖计算内容（不含判断ID/时间戳），用于版本去重与回放校验。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

from .calendar import TradingCalendar
from .clauses import (
    dependency_order,
    evaluate_maturity_clause,
    evaluate_window_clause,
)
from .models import OFFICIAL_SOURCES, ValidationError, money_str, parse_date, parse_money
from .price_schedule import build_schedule
from .store import Store


def _content_hash(payload: dict) -> str:
    # as_of_seq 是回放游标而非计算内容：无关记录到达会推进它，但证据并未变化，
    # 不参与哈希，避免产生结论相同的伪版本。
    body = {**payload,
            "manifest": {k: v for k, v in payload["manifest"].items() if k != "as_of_seq"}}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_inputs(store: Store, bond_id: str, as_of_seq: int | None = None) -> dict:
    """按 ingest 时点复原输入状态（as_of_seq=None 表示当前）。"""
    state = store.state_as_of(as_of_seq)
    terms = None
    calendars: list[dict] = []
    for rec in state:
        if rec["kind"] == "terms" and rec["entity_id"] == bond_id:
            terms = rec
        elif rec["kind"] == "calendar":
            calendars.append(rec)
    if terms is None:
        raise ValidationError(f"未找到债券 {bond_id!r} 的条款记录")
    calendar_id = terms.get("calendar_id")
    matches = [c for c in calendars if calendar_id is None or c["calendar_id"] == calendar_id]
    if not matches:
        raise ValidationError(f"未找到交易日历 {calendar_id or '(未指定)'!r} 的记录")
    if len(matches) > 1:
        raise ValidationError("存在多份交易日历，条款需指定 calendar_id")
    calendar_rec = matches[0]
    underlying = terms["underlying"]
    quotes: list[dict] = []
    suspensions: list[dict] = []
    actions: list[dict] = []
    resets: list[dict] = []
    for rec in state:
        if rec.get("status") == "withdrawn":
            continue  # 公告撤回：该实体最新版本已撤回，不参与计算
        if rec["kind"] == "quote" and rec["symbol"] == underlying:
            quotes.append(rec)
        elif rec["kind"] == "suspension" and rec["symbol"] == underlying:
            suspensions.append(rec)
        elif rec["kind"] == "corporate_action" and rec["symbol"] == underlying:
            actions.append(rec)
        elif rec["kind"] == "price_reset" and rec["bond_id"] == bond_id:
            resets.append(rec)
    return {
        "terms": terms,
        "calendar_record": calendar_rec,
        "calendar": TradingCalendar.from_record(calendar_rec),
        "quotes": quotes,
        "suspensions": suspensions,
        "corporate_actions": actions,
        "price_resets": resets,
    }


def evaluate_bond(store: Store, bond_id: str, valuation: date,
                  as_of_seq: int | None = None) -> dict:
    inputs = load_inputs(store, bond_id, as_of_seq)
    terms = inputs["terms"]
    calendar = inputs["calendar"]

    official: dict[date, dict] = {}
    temporary: dict[date, list[dict]] = {}
    for rec in inputs["quotes"]:
        day = parse_date(rec["trade_date"], "trade_date")
        if rec["price_source"] in OFFICIAL_SOURCES:
            prev = official.get(day)
            if prev is None or rec["record_version"] > prev["record_version"]:
                official[day] = rec
        else:
            temporary.setdefault(day, []).append(rec)

    susp_by_date = {parse_date(s["date"], "suspension.date"): s
                    for s in inputs["suspensions"]}

    schedule = build_schedule(
        anchor_date=parse_date(terms["conversion_period"]["start"], "conversion_period.start"),
        initial_price=parse_money(terms["initial_conversion_price"], "initial_conversion_price"),
        terms_source=terms["record_id"],
        corporate_actions=inputs["corporate_actions"],
        price_resets=inputs["price_resets"],
    )

    clauses = terms["clauses"]
    order = dependency_order(clauses)
    by_id = {c["clause_id"]: c for c in clauses}

    used_quotes: dict[str, dict] = {}
    trigger_dates: dict[str, date] = {}
    results: dict[str, dict] = {}
    for cid in order:
        clause = by_id[cid]
        if clause["type"] == "maturity":
            results[cid] = evaluate_maturity_clause(
                clause, terms=terms, valuation_date=valuation)
        else:
            results[cid] = evaluate_window_clause(
                clause,
                calendar=calendar,
                official=official,
                temporary=temporary,
                suspensions=susp_by_date,
                schedule=schedule,
                terms=terms,
                valuation_date=valuation,
                trigger_dates=trigger_dates,
                used_quotes=used_quotes,
            )
            trig = results[cid].get("trigger") or {}
            if trig.get("triggered") and trig.get("trigger_date"):
                trigger_dates[cid] = parse_date(trig["trigger_date"], "trigger_date")

    in_effect = [
        cid for cid in order
        if by_id[cid]["type"] != "maturity"
        and results[cid]["period"]["start"] <= valuation.isoformat() <= results[cid]["period"]["end"]
    ]
    effective_terms = {
        "terms_version": terms["record_version"],
        "terms_record_id": terms["record_id"],
        "conversion_price_at_valuation": money_str(schedule.price_at(valuation)),
        "price_schedule": schedule.to_evidence(),
        "clauses_in_effect": in_effect,
        "active_corporate_actions": [r["record_id"] for r in inputs["corporate_actions"]],
        "active_price_resets": [r["record_id"] for r in inputs["price_resets"]],
    }
    manifest = {
        "as_of_seq": as_of_seq if as_of_seq is not None else store.latest_record_seq,
        "terms_record_id": terms["record_id"],
        "terms_version": terms["record_version"],
        "calendar_record_id": inputs["calendar_record"]["record_id"],
        "quotes": dict(sorted(used_quotes.items())),
        "corporate_actions": {r["record_id"]: r["record_version"]
                              for r in inputs["corporate_actions"]},
        "suspensions": {r["record_id"]: r["record_version"]
                        for r in inputs["suspensions"]},
        "price_resets": {r["record_id"]: r["record_version"]
                         for r in inputs["price_resets"]},
    }
    content = {
        "bond_id": bond_id,
        "valuation_date": valuation.isoformat(),
        "dependency_order": order,
        "effective_terms": effective_terms,
        "clauses": results,
        "manifest": manifest,
    }
    content["content_hash"] = _content_hash(content)
    return content
