"""HTTP 端到端：seed → 评估 → 引用 → 补发行情 → 旧引用被圈出 → 回放。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from helpers import ROOT  # noqa: F401  (确保 sys.path 设置)

from app import create_server  # noqa: E402
from cb.service import CbService  # noqa: E402
from cb.store import Store  # noqa: E402

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        svc = CbService(Store(tempfile.mkdtemp()), REFERENCE_DIR)
        cls.server = create_server("127.0.0.1", 0, svc)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _req(self, method: str, path: str, body: object = None,
             expect: int = 200) -> dict:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(expect, resp.status, payload)
                return payload
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(expect, exc.code, payload)
            return payload

    def test_health(self) -> None:
        payload = self._req("GET", "/health")
        self.assertEqual("ok", payload["status"])

    def test_full_morning_story(self) -> None:
        # 载入样例数据
        seeded = self._req("POST", "/admin/seed", {}, expect=201)
        self.assertIn("terms", seeded["seeded"])

        # 晨会前评估：09-10/09-11 缺价、09-18 仅盘中临时价、08-20 停牌
        ev = self._req("POST", "/evaluate",
                       {"bond_id": "CB_DEMO", "valuation_date": "2026-09-18"},
                       expect=201)
        j1 = ev["judgment"]
        self.assertTrue(ev["created"])
        call = j1["clauses"]["call_130"]
        self.assertEqual("monitoring", call["status"])
        reasons = {e["date"]: e["reason"] for e in call["excluded"]}
        self.assertEqual("suspended", reasons["2026-08-20"])
        self.assertEqual("missing_price", reasons["2026-09-10"])
        self.assertEqual("temporary_only", reasons["2026-09-18"])
        # 转股价已被 7 月分红调整为 9.70
        self.assertEqual("9.70", j1["effective_terms"]["conversion_price_at_valuation"])
        # 距离触发：给出尚缺天数与候选日期
        self.assertGreater(call["summary"]["needed_to_trigger"], 0)
        self.assertTrue(call["projection"]["reachable"])

        # 客户经理引用该结论
        ref = self._req("POST", "/references",
                        {"judgment_id": j1["judgment_id"], "cited_by": "客户经理A"},
                        expect=201)
        self.assertEqual("R0001", ref["reference_id"])

        # 供应商补发两个交易日收盘价 → 自动生成新判断版本
        backfill = self._req("POST", "/quotes", [
            {"symbol": "600000", "trade_date": "2026-09-10",
             "close": "12.90", "price_source": "corrected"},
            {"symbol": "600000", "trade_date": "2026-09-11",
             "close": "12.95", "price_source": "corrected"},
        ], expect=201)
        self.assertEqual(["CB_DEMO:2026-09-18:v2"],
                         [r["judgment_id"] for r in backfill["reevaluated"]])

        # 旧引用被圈出，附差异摘要；旧版本本身未被覆盖
        refs = self._req("GET", "/bonds/CB_DEMO/references")["references"]
        self.assertTrue(refs[0]["stale"])
        self.assertEqual("CB_DEMO:2026-09-18:v2", refs[0]["current_judgment_id"])
        self.assertTrue(refs[0]["diff_summary"])
        old = self._req("GET", f"/judgments/{j1['judgment_id']}")
        self.assertEqual(1, old["version_no"])

        # 历史版本可逐日复核：回放重算与当时证据一致
        replay = self._req("POST", f"/judgments/{j1['judgment_id']}/replay")
        self.assertTrue(replay["matches"])

        # 盘中临时价之后正式收盘价到达 → 再生成新版本
        official = self._req("POST", "/quotes", [
            {"symbol": "600000", "trade_date": "2026-09-18",
             "close": "12.72", "price_source": "official_close"},
        ], expect=201)
        self.assertEqual(["CB_DEMO:2026-09-18:v3"],
                         [r["judgment_id"] for r in official["reevaluated"]])
        versions = self._req(
            "GET", "/bonds/CB_DEMO/judgments?valuation_date=2026-09-18")["judgments"]
        self.assertEqual([1, 2, 3], [v["version_no"] for v in versions])

    def test_unknown_bond_and_cycle_rejected(self) -> None:
        self._req("POST", "/evaluate",
                  {"bond_id": "NOPE", "valuation_date": "2026-09-18"}, expect=400)
        cycle = {
            "bond_id": "CYC", "name": "循环依赖", "underlying": "X",
            "issue_date": "2026-01-05", "maturity_date": "2030-01-05",
            "conversion_period": {"start": "2026-01-05", "end": "2030-01-05"},
            "initial_conversion_price": "10",
            "clauses": [
                {"clause_id": "a", "type": "redemption",
                 "window": {"kind": "m_of_n", "m": 2, "n": 3},
                 "condition": {"op": ">=", "pct": "1.3"},
                 "observe_until": {"trigger_of": "b"}},
                {"clause_id": "b", "type": "put",
                 "window": {"kind": "m_of_n", "m": 2, "n": 3},
                 "condition": {"op": "<=", "pct": "0.7"},
                 "observe_until": {"trigger_of": "a"}},
            ],
        }
        payload = self._req("POST", "/bonds", cycle, expect=422)
        self.assertIn("循环", payload["error"])


if __name__ == "__main__":
    unittest.main()
