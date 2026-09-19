"""日期、Decimal 与 JSON 的公共工具。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

ONE_DAY_DELTA = __import__("datetime").timedelta(days=1)


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def iso(d: date) -> str:
    return d.isoformat()


def dec(value: str | int | float | Decimal) -> Decimal:
    """把输入安全地转成 Decimal（拒绝 float 直接传入带来的精度噪声）。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(str(value))


def dec_str(d: Decimal) -> str:
    """定点输出，避免科学计数法，保证证据可读、可逐日复核。"""
    return format(d, "f")


def money_str(d: Decimal, max_precision: str = "0.0001") -> str:
    """金额展示：最多 4 位小数，去掉多余的尾零但至少保留两位（13.0000 -> 13.00）。"""
    quantized = d.quantize(Decimal(max_precision), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0")
        if text.endswith("."):
            text += "00"
        elif len(text.rsplit(".", 1)[1]) == 1:
            text += "0"
    else:
        text += ".00"
    return text


def quantize_price(d: Decimal, precision: str = "0.01") -> Decimal:
    return d.quantize(Decimal(precision), rounding=ROUND_HALF_UP)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
