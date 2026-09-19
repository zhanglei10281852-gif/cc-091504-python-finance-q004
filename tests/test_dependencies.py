from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cb.dependencies import ClauseDependencyError, evaluation_order


def clause(cid: str, ctype: str, **extra: object) -> dict:
    base: dict = {"clause_id": cid, "type": ctype}
    base.update(extra)
    return base


class DependencyTest(unittest.TestCase):
    def test_deterministic_order(self) -> None:
        clauses = [
            clause("maturity_1", "maturity"),
            clause("put_1", "put"),
            clause("call_1", "redemption", window_reset_on=["conversion_price_reset"]),
            clause("reset_1", "conversion_price_reset", modifies=["conversion_price"]),
        ]
        order = evaluation_order(clauses)
        self.assertEqual(["reset_1", "call_1", "put_1", "maturity_1"], order)
        # 输入顺序不影响结果
        again = evaluation_order(list(reversed(clauses)))
        self.assertEqual(order, again)

    def test_cycle_is_blocked(self) -> None:
        clauses = [
            clause("a", "redemption", depends_on=["b"]),
            clause("b", "put", depends_on=["a"]),
        ]
        with self.assertRaises(ClauseDependencyError) as ctx:
            evaluation_order(clauses)
        self.assertIn("a", ctx.exception.cycle)
        self.assertIn("b", ctx.exception.cycle)

    def test_self_dependency_blocked(self) -> None:
        clauses = [clause("a", "redemption", depends_on=["a"])]
        with self.assertRaises(ClauseDependencyError):
            evaluation_order(clauses)

    def test_unknown_reference_blocked(self) -> None:
        clauses = [clause("a", "redemption", depends_on=["ghost"])]
        with self.assertRaises(ClauseDependencyError):
            evaluation_order(clauses)

    def test_duplicate_clause_id_blocked(self) -> None:
        clauses = [clause("a", "redemption"), clause("a", "put")]
        with self.assertRaises(ClauseDependencyError):
            evaluation_order(clauses)

    def test_modifies_creates_edge(self) -> None:
        clauses = [
            clause("call_1", "redemption"),
            clause("reset_1", "conversion_price_reset", modifies=["conversion_price"]),
        ]
        # 强赎读取转股价，下修修改转股价 -> 下修必须先算
        self.assertEqual(["reset_1", "call_1"], evaluation_order(clauses))


if __name__ == "__main__":
    unittest.main()
