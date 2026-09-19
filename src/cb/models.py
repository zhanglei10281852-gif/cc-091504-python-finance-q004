"""基础类型与校验：日期、金额精度、枚举。

约定（与 reference/domain.json 一致）：
- 金额/价格在 JSON 中一律以字符串表示，内部用 Decimal，避免二进制浮点误差；
- 转股价保留两位小数（ROUND_HALF_UP），比值保留四位小数；
- 日期一律为 ISO 格式字符串，内部用 datetime.date。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CLAUSE_TYPES = ("redemption", "put", "conversion_price_reset", "maturity")
PRICE_SOURCES = ("official_close", "temporary", "corrected")
OFFICIAL_SOURCES = ("official_close", "corrected")
CORPORATE_ACTION_TYPES = ("cash_dividend", "stock_dividend", "rights_issue", "combo")
RECORD_STATUS = ("active", "withdrawn")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CENT = Decimal("0.01")
_RATIO = Decimal("0.0001")


class ValidationError(ValueError):
    """输入记录未通过校验。"""


def parse_date(value: object, field: str) -> date:
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        raise ValidationError(f"{field}: 需要 ISO 日期字符串，得到 {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field}: 非法日期 {value!r}") from exc


def parse_money(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValidationError(f"{field}: 需要数值，得到 {value!r}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field}: 无法解析数值 {value!r}") from exc
    if not result.is_finite():
        raise ValidationError(f"{field}: 数值必须有限，得到 {value!r}")
    return result


def q2(value: Decimal) -> Decimal:
    """价格精度：两位小数，四舍五入（ROUND_HALF_UP）。"""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def q4(value: Decimal) -> Decimal:
    """比值精度：四位小数。"""
    return value.quantize(_RATIO, rounding=ROUND_HALF_UP)


def money_str(value: Decimal) -> str:
    return str(q2(value))


def ratio_str(value: Decimal) -> str:
    return str(q4(value))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_fields(payload: dict, fields: list[str], kind: str) -> None:
    missing = [f for f in fields if payload.get(f) is None]
    if missing:
        raise ValidationError(f"{kind}: 缺少字段 {', '.join(missing)}")


def require_choice(value: object, choices: tuple[str, ...], field: str) -> str:
    if value not in choices:
        raise ValidationError(f"{field}: 仅支持 {list(choices)}，得到 {value!r}")
    return str(value)


def compare(op: str, left: Decimal, right: Decimal) -> bool:
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    raise ValidationError(f"condition.op: 不支持的比较符 {op!r}")
