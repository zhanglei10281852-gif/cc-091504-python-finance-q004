"""版本化：行情修订生成新判断版本、旧引用被圈出、历史版本可回放复原。"""
from __future__ import annotations

import unittest

from helpers import make_service, ingest_terms, quote


class VersioningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        ingest_terms(self.svc)
        # 01-08  initially 缺价；01-05 不达标，01-06/09/13 达标 → 3/5 触发
        self.svc.ingest_quotes([
            quote("2026-01-05", "12.00"),
            quote("2026-01-06", "13.10"),
            quote("2026-01-09", "13.20"),
            quote("2026-01-13", "13.30"),
        ])
        self.j1 = self.svc.evaluate("T1", "2026-01-13")["judgment"]

    def test_initial_triggered(self) -> None:
        self.assertEqual("triggered", self.j1["clauses"]["call"]["status"])
        self.assertEqual(1, self.j1["version_no"])

    def test_backfill_creates_new_version_and_flags_reference(self) -> None:
        self.svc.cite(self.j1["judgment_id"], "客户经理A", "晨会引用")
        result = self.svc.ingest_quotes([quote("2026-01-08", "11.00", "corrected")])
        self.assertEqual(["T1:2026-01-13:v2"],
                         [r["judgment_id"] for r in result["reevaluated"]])

        refs = self.svc.references("T1")
        self.assertEqual(1, len(refs))
        self.assertTrue(refs[0]["stale"])
        self.assertEqual("T1:2026-01-13:v2", refs[0]["current_judgment_id"])
        self.assertTrue(any("适用交易日数 4 → 5" in d for d in refs[0]["diff_summary"]))

        # 旧版本未被覆盖：证据仍是 4 个适用日、01-08 缺价
        old = self.svc.store.get_judgment("T1:2026-01-13:v1")
        self.assertEqual(4, old["clauses"]["call"]["summary"]["applicable_days"])
        reasons = {e["date"]: e["reason"] for e in old["clauses"]["call"]["excluded"]}
        self.assertEqual("missing_price", reasons["2026-01-08"])

    def test_correction_can_flip_conclusion(self) -> None:
        self.svc.cite(self.j1["judgment_id"], "客户经理A")
        # 01-09 由 13.20 更正为 12.00 → 达标数 3→2，结论翻转为未触发
        self.svc.ingest_quotes([quote("2026-01-09", "12.00", "corrected")])
        latest = self.svc.store.latest_judgment("T1", "2026-01-13")
        self.assertEqual(2, latest["version_no"])
        call = latest["clauses"]["call"]
        self.assertEqual("monitoring", call["status"])
        self.assertEqual(2, call["summary"]["hits_total"])

        refs = self.svc.references("T1")
        self.assertTrue(refs[0]["stale"])
        self.assertTrue(any("状态 triggered → monitoring" in d
                            for d in refs[0]["diff_summary"]))

    def test_same_value_correction_updates_provenance_only(self) -> None:
        # 同值更正：结论不变，但价格依据的记录版本变化 → 仍生成新版本留痕
        self.svc.cite(self.j1["judgment_id"], "客户经理A")
        result = self.svc.ingest_quotes([quote("2026-01-09", "13.20", "corrected")])
        self.assertEqual(["T1:2026-01-13:v2"],
                         [r["judgment_id"] for r in result["reevaluated"]])
        latest = self.svc.store.latest_judgment("T1", "2026-01-13")
        self.assertEqual("triggered", latest["clauses"]["call"]["status"])
        day09 = next(d for d in latest["clauses"]["call"]["days"]
                     if d["date"] == "2026-01-09")
        self.assertEqual(2, day09["record_version"])
        # 结论级差异为空，但引用仍被圈出（依据已变）
        refs = self.svc.references("T1")
        self.assertTrue(refs[0]["stale"])
        self.assertEqual([], refs[0]["diff_summary"])

    def test_temporary_quote_does_not_change_official_window(self) -> None:
        # 已有正式价的日期再收到盘中临时价：不产生新版本
        result = self.svc.ingest_quotes([quote("2026-01-09", "99.99", "temporary")])
        self.assertEqual([], result["reevaluated"])
        self.assertEqual(1, self.store_count())

    def store_count(self) -> int:
        return len(self.svc.store.judgments("T1", "2026-01-13"))

    def test_replay_restores_original_evidence(self) -> None:
        self.svc.ingest_quotes([quote("2026-01-08", "11.00", "corrected")])
        self.svc.ingest_quotes([quote("2026-01-09", "12.00", "corrected")])
        replay = self.svc.replay("T1:2026-01-13:v1")
        self.assertTrue(replay["matches"])
        self.assertEqual(replay["stored_hash"], replay["recomputed_hash"])
        # 回放看到的 01-09 仍是更正前的 13.20
        day09 = next(d for d in replay["evidence"]["clauses"]["call"]["days"]
                     if d["date"] == "2026-01-09")
        self.assertEqual("13.20", day09["close"])

    def test_manifest_pins_inputs(self) -> None:
        manifest = self.j1["manifest"]
        self.assertEqual(1, manifest["terms_version"])
        self.assertIn("2026-01-06", manifest["quotes"])
        self.assertEqual("terms:T1:v1", manifest["terms_record_id"])
        self.assertGreater(manifest["as_of_seq"], 0)


if __name__ == "__main__":
    unittest.main()
