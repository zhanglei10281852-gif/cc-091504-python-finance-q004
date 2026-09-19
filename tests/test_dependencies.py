"""条款依赖：确定性拓扑顺序、循环依赖拒绝、非法依赖校验。"""
from __future__ import annotations

import unittest

from helpers import TERMS, make_service, ingest_terms

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cb.clauses import (  # noqa: E402
    ClauseDependencyError,
    dependency_order,
    normalize_clause,
)
from cb.models import ValidationError  # noqa: E402


def _clause(cid: str, **extra) -> dict:
    base = {
        "clause_id": cid,
        "type": "redemption",
        "window": {"kind": "m_of_n", "m": 2, "n": 3},
        "condition": {"op": ">=", "pct": "1.30"},
    }
    base.update(extra)
    return normalize_clause(base)


class DependencyOrderTest(unittest.TestCase):
    def test_deterministic_order_by_id_when_independent(self) -> None:
        clauses = [_clause("c"), _clause("a"), _clause("b")]
        self.assertEqual(["a", "b", "c"], dependency_order(clauses))

    def test_trigger_dependency_orders_before_dependent(self) -> None:
        clauses = [
            _clause("z_put", observe_until={"trigger_of": "a_call"}),
            _clause("a_call"),
        ]
        self.assertEqual(["a_call", "z_put"], dependency_order(clauses))

    def test_explicit_trigger_dependency(self) -> None:
        clauses = [
            _clause("b", depends_on=["trigger:a"]),
            _clause("a"),
        ]
        self.assertEqual(["a", "b"], dependency_order(clauses))

    def test_cycle_is_rejected(self) -> None:
        clauses = [
            _clause("a", observe_until={"trigger_of": "b"}),
            _clause("b", observe_until={"trigger_of": "a"}),
        ]
        with self.assertRaises(ClauseDependencyError) as ctx:
            dependency_order(clauses)
        self.assertIn("循环", str(ctx.exception))

    def test_self_dependency_is_rejected(self) -> None:
        with self.assertRaises(ClauseDependencyError):
            dependency_order([_clause("a", observe_until={"trigger_of": "a"})])

    def test_unknown_clause_reference_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            dependency_order([_clause("a", observe_until={"trigger_of": "ghost"})])

    def test_unknown_dependency_resource_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            dependency_order([_clause("a", depends_on=["mystery"])])

    def test_terms_with_cycle_cannot_be_ingested(self) -> None:
        svc = make_service()
        payload = dict(TERMS)
        payload["clauses"] = [
            {"clause_id": "x", "type": "redemption",
             "window": {"kind": "m_of_n", "m": 2, "n": 3},
             "condition": {"op": ">=", "pct": "1.3"},
             "observe_until": {"trigger_of": "y"}},
            {"clause_id": "y", "type": "put",
             "window": {"kind": "m_of_n", "m": 2, "n": 3},
             "condition": {"op": "<=", "pct": "0.7"},
             "observe_until": {"trigger_of": "x"}},
        ]
        with self.assertRaises(ClauseDependencyError):
            svc.ingest_terms(payload)

    def test_engine_reports_dependency_order(self) -> None:
        svc = make_service()
        ingest_terms(svc)
        j = svc.evaluate("T1", "2026-01-05")["judgment"]
        order = j["dependency_order"]
        self.assertEqual(sorted(order), ["call", "mat", "put"])
        self.assertLess(order.index("call"), order.index("put"))


if __name__ == "__main__":
    unittest.main()
