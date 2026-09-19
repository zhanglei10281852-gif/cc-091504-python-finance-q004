"""窗口评估：适用交易日、停牌/缺价/临时价区分、m_of_n 与 all_of_n、触发距离推演。"""
from __future__ import annotations

import unittest

from helpers import make_service, ingest_terms, quote


class WindowEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        ingest_terms(self.svc)

    def _clause(self, judgment: dict, cid: str = "call") -> dict:
        return judgment["clauses"][cid]

    def test_trigger_with_excluded_days_classified(self) -> None:
        # 01-07 停牌，01-08 缺价，01-12 仅盘中临时价：均不计入窗口
        self.svc.ingest_suspension({"suspension_id": "S1", "symbol": "TST",
                                    "date": "2026-01-07", "reason": "测试停牌"})
        self.svc.ingest_quotes([
            quote("2026-01-05", "12.00"),           # 阈值 13.00，不达标
            quote("2026-01-06", "13.10"),           # 达标
            quote("2026-01-09", "13.20"),           # 达标
            quote("2026-01-12", "13.50", "temporary"),  # 盘中临时价
            quote("2026-01-13", "13.30"),           # 达标 → 3/5 触发
        ])
        j = self.svc.evaluate("T1", "2026-01-13")["judgment"]
        call = self._clause(j)
        self.assertEqual("triggered", call["status"])
        self.assertEqual("2026-01-13", call["trigger"]["trigger_date"])
        self.assertEqual(4, call["summary"]["applicable_days"])
        self.assertEqual(3, call["summary"]["hits_total"])

        excluded = {e["date"]: e["reason"] for e in call["excluded"]}
        self.assertEqual("suspended", excluded["2026-01-07"])
        self.assertEqual("missing_price", excluded["2026-01-08"])
        self.assertEqual("temporary_only", excluded["2026-01-12"])

        gaps = {g["date"]: g["reason"] for g in call["data_gaps"]}
        self.assertIn("2026-01-08", gaps)
        self.assertIn("2026-01-12", gaps)
        self.assertNotIn("2026-01-07", gaps)  # 停牌是事件，不是数据缺口

        # 每个适用日都留有价格比较证据
        day06 = next(d for d in call["days"] if d["date"] == "2026-01-06")
        self.assertEqual("13.10 >= 13.0000", day06["compare"])
        self.assertTrue(day06["hit"])
        self.assertEqual("10.00", day06["conversion_price"])

    def test_projection_before_trigger(self) -> None:
        self.svc.ingest_quotes([
            quote("2026-01-05", "12.00"),
            quote("2026-01-06", "13.10"),
            quote("2026-01-09", "13.20"),
        ])
        j = self.svc.evaluate("T1", "2026-01-09")["judgment"]
        call = self._clause(j)
        self.assertEqual("monitoring", call["status"])
        self.assertEqual(2, call["summary"]["hits_in_current_window"])
        self.assertEqual(1, call["summary"]["needed_to_trigger"])
        proj = call["projection"]
        self.assertTrue(proj["reachable"])
        self.assertEqual("2026-01-12", proj["earliest_trigger_date"])
        self.assertEqual(["2026-01-12"], proj["must_hit_dates"])

    def test_all_of_n_streak_survives_suspension(self) -> None:
        # put: 连续 3 个适用日 ≤ 7.00；中间停牌日不打断连续性
        self.svc.ingest_suspension({"suspension_id": "S2", "symbol": "TST",
                                    "date": "2026-01-07"})
        self.svc.ingest_quotes([
            quote("2026-01-05", "6.90"),
            quote("2026-01-06", "6.80"),
            quote("2026-01-08", "6.85"),
        ])
        j = self.svc.evaluate("T1", "2026-01-08")["judgment"]
        put = self._clause(j, "put")
        self.assertEqual("triggered", put["status"])
        self.assertEqual("2026-01-08", put["trigger"]["trigger_date"])
        self.assertEqual(3, put["summary"]["current_streak"])

    def test_observe_until_truncates_on_other_clause_trigger(self) -> None:
        # call 在 01-08 触发后，put 的观察期同日截断
        self.svc.ingest_quotes([
            quote("2026-01-05", "13.10"),
            quote("2026-01-06", "13.20"),
            quote("2026-01-08", "13.30"),
        ])
        j = self.svc.evaluate("T1", "2026-01-09")["judgment"]
        self.assertEqual("triggered", self._clause(j)["status"])
        put = self._clause(j, "put")
        self.assertEqual("2026-01-08", put["period"]["end"])
        self.assertEqual("trigger_of:call", put["period"]["end_reason"])

    def test_not_started_and_maturity(self) -> None:
        j = self.svc.evaluate("T1", "2025-12-31")["judgment"]
        self.assertEqual("not_started", self._clause(j)["status"])
        mat = self._clause(j, "mat")
        self.assertEqual("active", mat["status"])
        self.assertGreater(mat["days_to_maturity"], 0)

    def test_matured_and_calendar_gap(self) -> None:
        j = self.svc.evaluate("T1", "2030-01-06")["judgment"]
        mat = self._clause(j, "mat")
        self.assertEqual("matured", mat["status"])
        call = self._clause(j)
        self.assertEqual("ended_no_trigger", call["status"])
        gap_reasons = {g["reason"] for g in call["data_gaps"]}
        self.assertIn("calendar_not_covered", gap_reasons)

    def test_evaluate_is_idempotent_without_new_data(self) -> None:
        self.svc.ingest_quotes([quote("2026-01-05", "12.00")])
        first = self.svc.evaluate("T1", "2026-01-05")
        second = self.svc.evaluate("T1", "2026-01-05")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["judgment"]["judgment_id"],
                         second["judgment"]["judgment_id"])


if __name__ == "__main__":
    unittest.main()
