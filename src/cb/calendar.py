"""交易日历。"""
from __future__ import annotations

from datetime import date

from .models import parse_date


class TradingCalendar:
    def __init__(self, calendar_id: str, version: int, trading_days: list[date]):
        self.calendar_id = calendar_id
        self.version = version
        self._days = sorted(set(trading_days))
        self._set = set(self._days)

    @classmethod
    def from_record(cls, record: dict) -> "TradingCalendar":
        days = [parse_date(d, "trading_days") for d in record["trading_days"]]
        return cls(record["calendar_id"], record["record_version"], days)

    def is_trading_day(self, day: date) -> bool:
        return day in self._set

    @property
    def first_day(self) -> date | None:
        return self._days[0] if self._days else None

    @property
    def last_day(self) -> date | None:
        return self._days[-1] if self._days else None

    def days_between(self, start: date, end: date) -> list[date]:
        """[start, end] 区间内的交易日，含端点。"""
        return [d for d in self._days if start <= d <= end]

    def next_days(self, after: date, count: int, end: date | None = None) -> list[date]:
        """after 之后（不含）至多 count 个交易日，可选上界 end（含）。"""
        out = []
        for d in self._days:
            if d <= after:
                continue
            if end is not None and d > end:
                break
            out.append(d)
            if len(out) >= count:
                break
        return out
