"""晨会故事线的端到端测试：

1. 研究员基于基础行情评估 2026-09-18，强赎条件满足（15/30）；
2. 客户经理引用该判断版本；
3. 行情供应商补发两个交易日收盘价并修订一日价格；
4. 重新评估生成新版本（13/30，未满足），旧引用被圈出且附差异；
5. 旧版本证据完整保留，可随时复原当时所见。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cb.engine import add_reference, evaluate, scan_gaps
from cb.ingest import ingest_events, ingest_prices
from cb.seed import seed_if_empty
from cb.store import Store

REFERENCE_DIR = ROOT / "reference"
VALUATION_DATE = "2026-09-18"


def clause_of(judgment: dict, clause_id: str) -> dict:
    return next(c for c in judgment["clauses"] if c["clause_id"] == clause_id)


class EngineStoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        summary = seed_if_empty(self.store, REFERENCE_DIR)
        self.assertTrue(summary["seeded"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_v1_condition_met_with_classified_gaps(self) -> None:
        j1 = evaluate(self.store, "118000", VALUATION_DATE, now="2026-09-18T20:00:00+08:00")
        self.assertEqual(1, j1["version"])
        self.assertEqual("J118000-20260918-v1", j1["judgment_id"])
        self.assertEqual("10.00", j1["effective_conversion_price"])
        self.assertEqual(["reset_1", "call_1", "put_1", "maturity_1"], j1["dependency_order"])

        call = clause_of(j1, "call_1")
        self.assertEqual("condition_met", call["state"])
        self.assertEqual(30, call["window"]["counted_days"])
        self.assertEqual(15, call["window"]["hits"])
        self.assertEqual("2026-08-03", call["window"]["window_start"])
        self.assertEqual(0, call["remaining"]["hits_needed"])

        # 停牌、缺价、盘中临时价被分类排除，而不是悄悄跳过
        excluded = {d["date"]: d["status"] for d in call["excluded_days"]}
        self.assertEqual("suspended", excluded["2026-09-07"])
        self.assertEqual("suspended", excluded["2026-09-08"])
        self.assertEqual("missing_price", excluded["2026-09-03"])
        self.assertEqual("missing_price", excluded["2026-09-04"])
        self.assertEqual("temporary_only", excluded["2026-09-16"])
        gap_dates = {g["date"] for g in call["gaps"]}
        self.assertEqual({"2026-09-03", "2026-09-04", "2026-09-16"}, gap_dates)

        # 盘中临时价不参与正式判断：9/18 用官方收盘 13.10 而非临时价 13.50
        day_918 = next(d for d in call["days"] if d["date"] == "2026-09-18")
        self.assertEqual("13.10", day_918["close"])
        self.assertEqual("official_close", day_918["price_source"])
        self.assertEqual("13.00", day_918["threshold"])
        self.assertTrue(day_918["hit"])

        # 每个计入日都带完整比较痕迹
        for day in call["days"]:
            self.assertEqual("10.00", day["conversion_price"])
            self.assertIn(day["comparison"], (">=",))
            self.assertEqual(day["hit"], float(day["close"]) >= float(day["threshold"]))

        # 其他条款状态
        self.assertEqual("counting", clause_of(j1, "reset_1")["state"])
        self.assertEqual("not_applicable", clause_of(j1, "put_1")["state"])
        maturity = clause_of(j1, "maturity_1")
        self.assertEqual("active", maturity["state"])
        self.assertGreater(maturity["calendar_days_to_maturity"], 0)

    def test_correction_creates_new_version_and_flags_reference(self) -> None:
        j1 = evaluate(self.store, "118000", VALUATION_DATE, now="2026-09-18T20:00:00+08:00")
        ref = add_reference(
            self.store, j1["judgment_id"], consumer="客户经理-王某", note="晨会引用：强赎条件已满足"
        )
        self.assertEqual("current", ref["status"])

        # 供应商补发两个交易日 + 修订一日 + 补齐临时价日
        correction = json.loads((REFERENCE_DIR / "price_correction_20260919.json").read_text("utf-8"))
        result = ingest_prices(self.store, "600000", correction["bars"])
        self.assertEqual(1, len(result["superseded"]))  # 9/15 旧价被取代但保留
        self.assertEqual("2026-09-15", result["superseded"][0]["date"])

        j2 = evaluate(self.store, "118000", VALUATION_DATE, now="2026-09-19T08:30:00+08:00")
        self.assertEqual(2, j2["version"])
        self.assertEqual(j1["judgment_id"], j2["supersedes"])
        self.assertGreater(j2["data_version"], j1["data_version"])

        call2 = clause_of(j2, "call_1")
        self.assertEqual("counting", call2["state"])
        self.assertEqual(13, call2["window"]["hits"])
        self.assertEqual("2026-08-06", call2["window"]["window_start"])

        # 距离触发还差哪些日期
        self.assertEqual(2, call2["remaining"]["hits_needed"])
        self.assertEqual(["2026-09-21", "2026-09-22"], call2["remaining"]["needed_dates"])
        self.assertEqual("2026-09-22", call2["remaining"]["earliest_trigger_date"])

        # 9/15 使用修订价 13.09；9/16 用补发的官方价而非盘中临时价
        days = {d["date"]: d for d in call2["days"]}
        self.assertEqual("13.09", days["2026-09-15"]["close"])
        self.assertEqual("corrected", days["2026-09-15"]["price_source"])
        self.assertEqual("12.97", days["2026-09-16"]["close"])
        self.assertFalse(days["2026-09-16"]["hit"])

        # 引用被圈出，附差异，不被悄悄覆盖
        stale = self.store.list_references(status="stale")
        self.assertEqual(1, len(stale))
        self.assertEqual(j1["judgment_id"], stale[0]["judgment_id"])
        self.assertEqual(j2["judgment_id"], stale[0]["superseded_by"])
        diff_text = "\n".join(stale[0]["diff"])
        self.assertIn("condition_met", diff_text)
        self.assertIn("counting", diff_text)
        self.assertIn("15", diff_text)
        self.assertIn("13", diff_text)

        # 历史引用可复原当时看到的完整证据
        restored = self.store.get_judgment(j1["judgment_id"])
        self.assertIsNotNone(restored)
        self.assertEqual(15, clause_of(restored, "call_1")["window"]["hits"])
        self.assertEqual(j1["evidence_hash"], restored["evidence_hash"])
        self.assertEqual(30, len(clause_of(restored, "call_1")["days"]))

    def test_reset_announcement_and_retraction(self) -> None:
        # 公告下修：转股价 10.00 -> 9.00，阈值随之变化
        ingest_events(
            self.store,
            "118000",
            [
                {
                    "event_id": "EV-RESET-20260910",
                    "event_type": "reset",
                    "effective_date": "2026-09-10",
                    "params": {"new_price": "9.00"},
                }
            ],
        )
        j_reset = evaluate(self.store, "118000", VALUATION_DATE)
        call = clause_of(j_reset, "call_1")
        self.assertEqual("9.00", j_reset["effective_conversion_price"])
        # 观察期自下修生效日重起：9/10 起适用交易日不足 30 天
        self.assertEqual("accumulating", call["state"])
        self.assertEqual("2026-09-10", call["window"]["window_reset_after"])
        day = call["days"][0]
        self.assertEqual("11.70", day["threshold"])  # 9.00 * 1.30

        # 公告撤回：下修失效，转股价与观察期恢复
        ingest_events(
            self.store,
            "118000",
            [
                {
                    "event_id": "EV-RETRACT-20260915",
                    "event_type": "retraction",
                    "params": {"target_event_id": "EV-RESET-20260910", "reason": "董事会决议撤销"},
                }
            ],
        )
        j_back = evaluate(self.store, "118000", VALUATION_DATE)
        self.assertEqual("10.00", j_back["effective_conversion_price"])
        call_back = clause_of(j_back, "call_1")
        # 观察期重起点回落到仍然有效的 7/15 下修（不影响 8 月起的窗口）
        self.assertEqual("2026-07-15", call_back["window"]["window_reset_after"])
        self.assertEqual("condition_met", call_back["state"])
        # 撤回事件本身留在证据里
        self.assertEqual(
            ["EV-RESET-20260910"], [e["event_id"] for e in j_back["retracted_events"]]
        )

    def test_gaps_scan(self) -> None:
        gaps = scan_gaps(self.store, "118000", VALUATION_DATE)
        gap_map = {g["date"]: g["status"] for g in gaps["gaps"]}
        self.assertEqual("missing_price", gap_map["2026-09-03"])
        self.assertEqual("temporary_only", gap_map["2026-09-16"])
        susp_dates = {s["date"] for s in gaps["suspensions"]}
        self.assertEqual({"2026-09-07", "2026-09-08"}, susp_dates)

    def test_non_trading_valuation_date_floors(self) -> None:
        # 周六估值 -> 落到周五 9/18
        j = evaluate(self.store, "118000", "2026-09-19")
        call = clause_of(j, "call_1")
        self.assertEqual("2026-09-18", call["as_of_date"])
        self.assertEqual("2026-09-18", call["window"]["window_end"])


if __name__ == "__main__":
    unittest.main()
