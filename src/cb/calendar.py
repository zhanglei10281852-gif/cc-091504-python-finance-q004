"""交易日历：由日期范围 + 周末规则 + 节假日列表推导，确定且可复核。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .util import parse_date

_ONE_DAY = timedelta(days=1)


class TradingCalendar:
    def __init__(
        self,
        calendar_id: str,
        start: date,
        end: date,
        holidays: list[date] | None = None,
        weekends: tuple[int, ...] = (5, 6),
    ) -> None:
        self.calendar_id = calendar_id
        self.start = start
        self.end = end
        self.holidays = frozenset(holidays or [])
        self.weekends = weekends
        days: list[date] = []
        d = start
        while d <= end:
            if d.weekday() not in self.weekends and d not in self.holidays:
                days.append(d)
            d += _ONE_DAY
        self._days = days
        self._set = frozenset(days)

    def is_trading_day(self, d: date) -> bool:
        return d in self._set

    def next_trading_day(self, d: date) -> date | None:
        """严格晚于 d 的下一个交易日。"""
        cur = d + _ONE_DAY
        while cur <= self.end:
            if cur in self._set:
                return cur
            cur += _ONE_DAY
        return None

    def prev_trading_day(self, d: date) -> date | None:
        """严格早于 d 的上一个交易日。"""
        cur = d - _ONE_DAY
        while cur >= self.start:
            if cur in self._set:
                return cur
            cur -= _ONE_DAY
        return None

    def floor(self, d: date) -> date | None:
        """d 当日（若为交易日）否则之前最近交易日。"""
        if d in self._set:
            return d
        return self.prev_trading_day(d)

    def kth_trading_day_after(self, d: date, k: int) -> date | None:
        cur = d
        for _ in range(k):
            nxt = self.next_trading_day(cur)
            if nxt is None:
                return None
            cur = nxt
        return cur

    def trading_days_between(self, a: date, b: date) -> list[date]:
        return [d for d in self._days if a <= d <= b]

    def count_trading_days_between(self, a: date, b: date) -> int:
        return sum(1 for d in self._days if a <= d <= b)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calendar_id": self.calendar_id,
            "range": [self.start.isoformat(), self.end.isoformat()],
            "holidays": sorted(d.isoformat() for d in self.holidays),
            "weekends": list(self.weekends),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TradingCalendar":
        start_s, end_s = data["range"]
        return cls(
            calendar_id=data["calendar_id"],
            start=parse_date(start_s),
            end=parse_date(end_s),
            holidays=[parse_date(h) for h in data.get("holidays", [])],
            weekends=tuple(data.get("weekends", (5, 6))),
        )
