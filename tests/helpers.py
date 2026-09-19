"""测试公共夹具：小规模手工可算的条款、日历与行情。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cb.service import CbService  # noqa: E402
from cb.store import Store  # noqa: E402

# 2026-01-05(周一) 起连续 4 周工作日，无节假日
CALENDAR = {
    "calendar_id": "TEST",
    "trading_days": [
        "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09",
        "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15", "2026-01-16",
        "2026-01-19", "2026-01-20", "2026-01-21", "2026-01-22", "2026-01-23",
        "2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30",
    ],
}

TERMS = {
    "bond_id": "T1",
    "name": "测试转债",
    "underlying": "TST",
    "issue_date": "2026-01-05",
    "maturity_date": "2030-01-05",
    "maturity_redemption_price": "108.00",
    "conversion_period": {"start": "2026-01-05", "end": "2030-01-05"},
    "initial_conversion_price": "10.00",
    "clauses": [
        {
            "clause_id": "call",
            "type": "redemption",
            "window": {"kind": "m_of_n", "m": 3, "n": 5},
            "condition": {"op": ">=", "pct": "1.30"},
            "effective": {"from": "conversion_period_start", "to": "maturity"},
        },
        {
            "clause_id": "put",
            "type": "put",
            "window": {"kind": "all_of_n", "n": 3},
            "condition": {"op": "<=", "pct": "0.70"},
            "effective": {"from": "conversion_period_start", "to": "maturity"},
            "observe_until": {"trigger_of": "call"},
        },
        {"clause_id": "mat", "type": "maturity"},
    ],
}


def make_service(reference_dir: str | None = None) -> CbService:
    store = Store(tempfile.mkdtemp())
    svc = CbService(store, reference_dir)
    svc.ingest_calendar(CALENDAR)
    return svc


def ingest_terms(svc: CbService, **overrides) -> dict:
    import copy
    payload = copy.deepcopy(TERMS)
    payload.update(overrides)
    return svc.ingest_terms(payload)


def quote(date: str, close: str, source: str = "official_close") -> dict:
    return {"symbol": "TST", "trade_date": date, "close": close, "price_source": source}
