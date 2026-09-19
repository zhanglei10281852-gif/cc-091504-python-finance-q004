"""领域模型：债券条款、行情记录、事件。

设计约定：
- 所有金额/价格用 Decimal，JSON 中序列化为字符串，杜绝浮点误差进入证据；
- 每条记录区分业务时间（effective_date / bar_date）与系统接收时间（received_at），
  并通过 status（active / superseded / retracted）保留完整版本链；
- 条款（Clause）以 dict 承载，属于"可版本化的规则配置"，字段见 README 与样例。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from .util import dec, dec_str, iso, parse_date

# 行情来源：官方收盘 / 盘中临时 / 供应商修订
PRICE_SOURCES = ("official_close", "temporary", "corrected")
# 可用于正式判断的行情来源（盘中临时数据被排除）
OFFICIAL_SOURCES = ("official_close", "corrected")

BAR_ACTIVE = "active"
BAR_SUPERSEDED = "superseded"  # 被更新的修订取代，保留可查
BAR_RETRACTED = "retracted"  # 供应商撤回

EVENT_ACTIVE = "active"
EVENT_RETRACTED = "retracted"  # 公告撤回，事件本身保留

# 事件类型：除权除息 / 下修 / 停牌 / 公告撤回
EVENT_TYPES = (
    "cash_dividend",
    "bonus_share",
    "rights_issue",
    "reset",
    "suspension",
    "retraction",
)

CLAUSE_TYPES = ("redemption", "put", "conversion_price_reset", "maturity")


@dataclass
class PriceBar:
    stock_code: str
    bar_date: date
    close: Decimal
    source: str
    revision: int
    received_at: str
    status: str = BAR_ACTIVE
    bar_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar_id": self.bar_id,
            "stock_code": self.stock_code,
            "date": iso(self.bar_date),
            "close": dec_str(self.close),
            "source": self.source,
            "revision": self.revision,
            "received_at": self.received_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PriceBar":
        return cls(
            stock_code=data["stock_code"],
            bar_date=parse_date(data["date"]),
            close=dec(data["close"]),
            source=data["source"],
            revision=int(data["revision"]),
            received_at=data["received_at"],
            status=data.get("status", BAR_ACTIVE),
            bar_id=data.get("bar_id", ""),
        )


@dataclass
class Event:
    """公司行动 / 公告事件。params 按 event_type 解释：

    - cash_dividend: {amount}                每股派息 D
    - bonus_share:   {ratio}                 每股送/转 n
    - rights_issue:  {ratio, price}          每股配/增发 k、价格 A
    - reset:         {new_price, clause_id?} 下修后转股价
    - suspension:    {start_date, end_date, reason}
    - retraction:    {target_event_id, reason?}  公告撤回
    """

    event_id: str
    bond_id: str
    event_type: str
    params: dict[str, Any]
    received_at: str
    effective_date: date | None = None
    status: str = EVENT_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "bond_id": self.bond_id,
            "event_type": self.event_type,
            "params": self.params,
            "effective_date": iso(self.effective_date) if self.effective_date else None,
            "received_at": self.received_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        eff = data.get("effective_date")
        return cls(
            event_id=data["event_id"],
            bond_id=data["bond_id"],
            event_type=data["event_type"],
            params=dict(data.get("params", {})),
            received_at=data["received_at"],
            effective_date=parse_date(eff) if eff else None,
            status=data.get("status", EVENT_ACTIVE),
        )


@dataclass
class Bond:
    bond_id: str
    name: str
    stock_code: str
    calendar_id: str
    list_date: date
    maturity_date: date
    conversion_period: tuple[date, date]
    initial_conversion_price: Decimal
    clauses: list[dict[str, Any]] = field(default_factory=list)
    price_precision: str = "0.01"

    def clause(self, clause_id: str) -> dict[str, Any] | None:
        for c in self.clauses:
            if c.get("clause_id") == clause_id:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bond_id": self.bond_id,
            "name": self.name,
            "stock_code": self.stock_code,
            "calendar_id": self.calendar_id,
            "list_date": iso(self.list_date),
            "maturity_date": iso(self.maturity_date),
            "conversion_period": [iso(self.conversion_period[0]), iso(self.conversion_period[1])],
            "initial_conversion_price": dec_str(self.initial_conversion_price),
            "price_precision": self.price_precision,
            "clauses": self.clauses,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Bond":
        period = data.get("conversion_period") or [data["list_date"], data["maturity_date"]]
        return cls(
            bond_id=data["bond_id"],
            name=data.get("name", data["bond_id"]),
            stock_code=data["stock_code"],
            calendar_id=data.get("calendar_id", "CN_equity"),
            list_date=parse_date(data["list_date"]),
            maturity_date=parse_date(data["maturity_date"]),
            conversion_period=(parse_date(period[0]), parse_date(period[1])),
            initial_conversion_price=dec(data["initial_conversion_price"]),
            clauses=[dict(c) for c in data.get("clauses", [])],
            price_precision=data.get("price_precision", "0.01"),
        )
