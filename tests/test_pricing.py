from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cb.models import Bond, Event
from cb.pricing import build_price_timeline, price_on


def make_bond() -> Bond:
    return Bond.from_dict(
        {
            "bond_id": "T1",
            "stock_code": "600000",
            "list_date": "2026-01-05",
            "maturity_date": "2032-01-04",
            "initial_conversion_price": "10.00",
            "clauses": [],
        }
    )


def ev(event_id: str, etype: str, eff: str, params: dict, status: str = "active") -> Event:
    return Event(
        event_id=event_id,
        bond_id="T1",
        event_type=etype,
        params=params,
        received_at="2026-01-01T00:00:00+00:00",
        effective_date=date.fromisoformat(eff),
        status=status,
    )


class PricingTest(unittest.TestCase):
    def test_cash_dividend(self) -> None:
        bond = make_bond()
        timeline = build_price_timeline(bond, [ev("E1", "cash_dividend", "2026-06-22", {"amount": "0.30"})])
        self.assertEqual(Decimal("10.00"), price_on(timeline, date(2026, 6, 21)))
        self.assertEqual(Decimal("9.70"), price_on(timeline, date(2026, 6, 22)))

    def test_bonus_share(self) -> None:
        bond = make_bond()
        timeline = build_price_timeline(bond, [ev("E1", "bonus_share", "2026-06-22", {"ratio": "0.5"})])
        # 10 / 1.5 = 6.666... -> 6.67
        self.assertEqual(Decimal("6.67"), price_on(timeline, date(2026, 6, 22)))

    def test_rights_issue(self) -> None:
        bond = make_bond()
        timeline = build_price_timeline(
            bond, [ev("E1", "rights_issue", "2026-06-22", {"ratio": "0.2", "price": "5.00"})]
        )
        # (10 + 5*0.2) / 1.2 = 9.1666... -> 9.17
        self.assertEqual(Decimal("9.17"), price_on(timeline, date(2026, 6, 22)))

    def test_combined_same_day(self) -> None:
        bond = make_bond()
        events = [
            ev("E1", "cash_dividend", "2026-06-22", {"amount": "0.30"}),
            ev("E2", "bonus_share", "2026-06-22", {"ratio": "0.5"}),
            ev("E3", "rights_issue", "2026-06-22", {"ratio": "0.2", "price": "5.00"}),
        ]
        timeline = build_price_timeline(bond, events)
        # (10 - 0.3 + 5*0.2) / (1 + 0.5 + 0.2) = 10.7 / 1.7 = 6.2941... -> 6.29
        self.assertEqual(Decimal("6.29"), price_on(timeline, date(2026, 6, 22)))

    def test_reset_overrides_and_chains(self) -> None:
        bond = make_bond()
        events = [
            ev("E1", "cash_dividend", "2026-06-22", {"amount": "0.30"}),
            ev("E2", "reset", "2026-07-15", {"new_price": "8.80"}),
            ev("E3", "cash_dividend", "2026-08-01", {"amount": "0.20"}),
        ]
        timeline = build_price_timeline(bond, events)
        self.assertEqual(Decimal("8.80"), price_on(timeline, date(2026, 7, 15)))
        # 下修后的派息以下修价为基数
        self.assertEqual(Decimal("8.60"), price_on(timeline, date(2026, 8, 1)))

    def test_retracted_event_excluded(self) -> None:
        bond = make_bond()
        events = [
            ev("E1", "reset", "2026-07-15", {"new_price": "8.80"}),
            ev("E2", "reset", "2026-08-24", {"new_price": "8.00"}, status="retracted"),
        ]
        timeline = build_price_timeline(bond, events)
        self.assertEqual(Decimal("8.80"), price_on(timeline, date(2026, 9, 1)))
        causes = [seg["cause"] for seg in timeline]
        self.assertEqual(["initial", "reset"], causes)

    def test_price_before_timeline_start_raises(self) -> None:
        bond = make_bond()
        timeline = build_price_timeline(bond, [])
        with self.assertRaises(ValueError):
            price_on(timeline, date(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
