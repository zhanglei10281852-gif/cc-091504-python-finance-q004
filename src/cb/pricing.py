"""转股价时间线：初始价 + 除权除息调整 + 下修，逐段生效。

调整公式（与可转债募集说明书通行口径一致）：
- 派送股票股利/转增股本 n（每股）：      P1 = P0 / (1 + n)
- 增发新股/配股 k（每股）、价格 A：       P1 = (P0 + A·k) / (1 + k)
- 派息 D（每股）：                        P1 = P0 − D
- 同日多项合并：                          P1 = (P0 − D + A·k) / (1 + n + k)

同一生效日若同时存在除权事件与下修，先按除权公式调整，再应用下修给定的新价
（下修公告中的新转股价本身已考虑当日除权）。被撤回的事件不参与计算，
但在判断证据中以 retracted_events 单独列出。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .models import Bond, Event
from .util import dec, dec_str, quantize_price

CORPORATE_ACTION_TYPES = ("cash_dividend", "bonus_share", "rights_issue")


def _apply_corporate_actions(p0: Decimal, actions: list[Event], precision: str) -> Decimal:
    n = Decimal("0")  # 送股/转增合计
    k = Decimal("0")  # 配股/增发合计
    a_times_k = Decimal("0")  # Σ A·k
    dividend = Decimal("0")  # 派息合计
    for ev in actions:
        params = ev.params
        if ev.event_type == "cash_dividend":
            dividend += dec(params["amount"])
        elif ev.event_type == "bonus_share":
            n += dec(params["ratio"])
        elif ev.event_type == "rights_issue":
            ratio = dec(params["ratio"])
            k += ratio
            a_times_k += dec(params["price"]) * ratio
        else:  # pragma: no cover - 防御
            raise ValueError(f"非除权事件类型: {ev.event_type}")
    denominator = Decimal("1") + n + k
    if denominator <= 0:
        raise ValueError("除权参数导致分母非正")
    p1 = (p0 - dividend + a_times_k) / denominator
    return quantize_price(p1, precision)


def build_price_timeline(bond: Bond, events: list[Event]) -> list[dict[str, Any]]:
    """返回按生效日升序的分段列表，每段 {effective_date, price, cause, event_ids}。"""
    active = [e for e in events if e.status == "active"]
    by_date: dict[date, dict[str, list[Event]]] = {}
    for ev in active:
        if ev.event_type in CORPORATE_ACTION_TYPES and ev.effective_date:
            by_date.setdefault(ev.effective_date, {}).setdefault("actions", []).append(ev)
        elif ev.event_type == "reset" and ev.effective_date:
            by_date.setdefault(ev.effective_date, {}).setdefault("resets", []).append(ev)

    segments: list[dict[str, Any]] = [
        {
            "effective_date": bond.list_date.isoformat(),
            "price": dec_str(bond.initial_conversion_price),
            "cause": "initial",
            "event_ids": [],
        }
    ]
    current = bond.initial_conversion_price
    for eff_date in sorted(by_date):
        group = by_date[eff_date]
        actions = group.get("actions", [])
        resets = group.get("resets", [])
        if actions:
            current = _apply_corporate_actions(current, actions, bond.price_precision)
            segments.append(
                {
                    "effective_date": eff_date.isoformat(),
                    "price": dec_str(current),
                    "cause": "corporate_action",
                    "event_ids": [e.event_id for e in actions],
                }
            )
        for ev in sorted(resets, key=lambda e: e.received_at):
            current = quantize_price(dec(ev.params["new_price"]), bond.price_precision)
            segments.append(
                {
                    "effective_date": eff_date.isoformat(),
                    "price": dec_str(current),
                    "cause": "reset",
                    "event_ids": [ev.event_id],
                }
            )
    return segments


def price_on(timeline: list[dict[str, Any]], d: date) -> Decimal:
    """取 d 当日有效的转股价（最后一个生效日 <= d 的分段）。"""
    chosen: Decimal | None = None
    for seg in timeline:
        if date.fromisoformat(seg["effective_date"]) <= d:
            chosen = dec(seg["price"])
        else:
            break
    if chosen is None:
        raise ValueError(f"{d} 早于转股价时间线起点")
    return chosen
