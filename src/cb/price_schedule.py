"""转股价时间线：初始价 + 除权事件调整 + 已批准下修。

转股价只能被"记录"改变（除权事件、下修公告），条款评估本身不改价——
条款触发与价格生效在现实中也以发行人公告为界。所有事件按
(生效日, 类型优先级, 记录ID) 确定性排序依次作用：
- 除权事件按公式调整当前价（同一除权日多个事件按记录ID顺序复合）；
- 下修为绝对定价，同一日排在除权事件之后（即同日下修覆盖当日调整结果）。
每次调整后价格按两位小数 ROUND_HALF_UP 落位。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .models import ValidationError, money_str, parse_date, parse_money, q2

_EVENT_PRIORITY = {"terms": 0, "corporate_action": 1, "price_reset": 2}


def adjust_price(p0: Decimal, action_type: str, params: dict) -> Decimal:
    """除权调整公式（募集说明书标准形式）。"""
    if action_type == "cash_dividend":
        return p0 - parse_money(params.get("D"), "params.D")
    if action_type == "stock_dividend":
        n = parse_money(params.get("n"), "params.n")
        return p0 / (Decimal(1) + n)
    if action_type == "rights_issue":
        a = parse_money(params.get("A"), "params.A")
        k = parse_money(params.get("k"), "params.k")
        return (p0 + a * k) / (Decimal(1) + k)
    if action_type == "combo":
        d = parse_money(params.get("D", "0"), "params.D")
        a = parse_money(params.get("A", "0"), "params.A")
        k = parse_money(params.get("k", "0"), "params.k")
        n = parse_money(params.get("n", "0"), "params.n")
        return (p0 - d + a * k) / (Decimal(1) + n + k)
    raise ValidationError(f"action_type: 不支持的除权类型 {action_type!r}")


class PriceSchedule:
    """分段常数的转股价函数，price_at(d) 取 d 当日（含）之前最后一个事件的价格。"""

    def __init__(self, entries: list[dict]):
        self.entries = entries  # [{date, price(Decimal), source, kind}]，已排序

    def price_at(self, day: date) -> Decimal:
        price = self.entries[0]["price"]
        for entry in self.entries:
            if entry["date"] <= day:
                price = entry["price"]
            else:
                break
        return price

    def to_evidence(self) -> list[dict]:
        return [
            {
                "date": e["date"].isoformat(),
                "price": money_str(e["price"]),
                "source": e["source"],
                "kind": e["kind"],
            }
            for e in self.entries
        ]


def build_schedule(
    anchor_date: date,
    initial_price: Decimal,
    terms_source: str,
    corporate_actions: list[dict],
    price_resets: list[dict],
) -> PriceSchedule:
    events: list[tuple[date, int, str, str, Decimal | None, dict | None]] = []
    for rec in corporate_actions:
        ex_date = parse_date(rec["ex_date"], "ex_date")
        events.append((ex_date, _EVENT_PRIORITY["corporate_action"], rec["record_id"],
                       "corporate_action", None, rec))
    for rec in price_resets:
        eff = parse_date(rec["effective_date"], "effective_date")
        events.append((eff, _EVENT_PRIORITY["price_reset"], rec["record_id"],
                       "price_reset", parse_money(rec["new_price"], "new_price"), rec))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    entries: list[dict] = [{
        "date": anchor_date,
        "price": q2(initial_price),
        "source": terms_source,
        "kind": "terms",
    }]
    current = q2(initial_price)
    for eff_date, _prio, record_id, kind, absolute, rec in events:
        if kind == "corporate_action":
            current = q2(adjust_price(current, rec["action_type"], rec.get("params", {})))
        else:
            current = q2(absolute)  # type: ignore[arg-type]
        entries.append({"date": eff_date, "price": current, "source": record_id, "kind": kind})
    return PriceSchedule(entries)
