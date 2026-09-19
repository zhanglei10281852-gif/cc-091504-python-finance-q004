"""转股价时间线：除权公式、下修覆盖、同日优先级、公告撤回。"""
from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from helpers import make_service, ingest_terms, quote

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cb.price_schedule import adjust_price, build_schedule  # noqa: E402


class AdjustFormulaTest(unittest.TestCase):
    def test_cash_dividend(self) -> None:
        self.assertEqual(Decimal("9.70"), adjust_price(Decimal("10.00"), "cash_dividend",
                                                       {"D": "0.30"}))

    def test_stock_dividend(self) -> None:
        self.assertEqual(Decimal("7.6923") , adjust_price(Decimal("10.00"), "stock_dividend",
                                                          {"n": "0.3"}).quantize(Decimal("0.0001")))

    def test_rights_issue(self) -> None:
        # (10 + 8*0.2) / 1.2 = 9.6666...
        self.assertEqual(Decimal("9.6667"),
                         adjust_price(Decimal("10.00"), "rights_issue",
                                      {"A": "8", "k": "0.2"}).quantize(Decimal("0.0001")))

    def test_combo(self) -> None:
        # (10 - 0.3 + 8*0.2) / (1 + 0.3 + 0.2) = 11.3 / 1.5 = 7.5333...
        self.assertEqual(Decimal("7.5333"),
                         adjust_price(Decimal("10.00"), "combo",
                                      {"D": "0.3", "A": "8", "k": "0.2", "n": "0.3"})
                         .quantize(Decimal("0.0001")))


def _ca(record_id: str, ex_date: str, action_type: str, params: dict) -> dict:
    return {"record_id": record_id, "ex_date": ex_date,
            "action_type": action_type, "params": params}


def _reset(record_id: str, effective_date: str, new_price: str) -> dict:
    return {"record_id": record_id, "effective_date": effective_date, "new_price": new_price}


class ScheduleTest(unittest.TestCase):
    def test_chronological_composition_and_same_day_priority(self) -> None:
        schedule = build_schedule(
            anchor_date=date(2026, 1, 5),
            initial_price=Decimal("10.00"),
            terms_source="terms:T1:v1",
            corporate_actions=[
                _ca("corporate_action:CA1:v1", "2026-01-10", "cash_dividend", {"D": "0.30"}),
                _ca("corporate_action:CA2:v1", "2026-01-13", "stock_dividend", {"n": "0.3"}),
            ],
            price_resets=[_reset("price_reset:R1:v1", "2026-01-10", "9.50")],
        )
        self.assertEqual(Decimal("10.00"), schedule.price_at(date(2026, 1, 9)))
        # 同日：先除权(10.00→9.70)，后下修覆盖为 9.50
        self.assertEqual(Decimal("9.50"), schedule.price_at(date(2026, 1, 10)))
        # 后续除权作用于下修后的价格：9.50 / 1.3 = 7.31（两位小数落位）
        self.assertEqual(Decimal("7.31"), schedule.price_at(date(2026, 1, 13)))

    def test_withdrawn_reset_removed_by_service(self) -> None:
        svc = make_service()
        ingest_terms(svc)
        svc.ingest_quotes([quote("2026-01-12", "9.60")])
        svc.ingest_price_reset({"reset_id": "RS1", "bond_id": "T1",
                                "effective_date": "2026-01-12", "new_price": "9.50"})
        j = svc.evaluate("T1", "2026-01-12")["judgment"]
        day = j["clauses"]["call"]["days"][0]
        self.assertEqual("9.50", day["conversion_price"])

        # 撤回下修公告 → 重评估 → 转股价回到 10.00
        svc.ingest_price_reset({"reset_id": "RS1", "bond_id": "T1",
                                "effective_date": "2026-01-12", "new_price": "9.50",
                                "status": "withdrawn"})
        latest = svc.store.latest_judgment("T1", "2026-01-12")
        self.assertEqual(2, latest["version_no"])
        day = latest["clauses"]["call"]["days"][0]
        self.assertEqual("10.00", day["conversion_price"])
        # 旧版本证据不变
        old = svc.store.get_judgment("T1:2026-01-12:v1")
        self.assertEqual("9.50", old["clauses"]["call"]["days"][0]["conversion_price"])


if __name__ == "__main__":
    unittest.main()
