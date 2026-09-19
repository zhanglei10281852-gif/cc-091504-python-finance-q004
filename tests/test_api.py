from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import create_server
from cb.seed import seed_if_empty
from cb.store import Store

REFERENCE_DIR = ROOT / "reference"


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)
        seed_if_empty(self.store, REFERENCE_DIR)
        self.server = create_server("127.0.0.1", 0, store=self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self) -> None:
        status, payload = self.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_full_morning_meeting_flow(self) -> None:
        # 1. 研究员评估
        status, j1 = self.call("POST", "/bonds/118000/evaluate", {"date": "2026-09-18"})
        self.assertEqual(201, status)
        call1 = next(c for c in j1["clauses"] if c["clause_id"] == "call_1")
        self.assertEqual("condition_met", call1["state"])

        # 2. 客户经理引用
        status, ref = self.call(
            "POST",
            "/references",
            {"judgment_id": j1["judgment_id"], "consumer": "客户经理-王某"},
        )
        self.assertEqual(201, status)
        self.assertEqual("current", ref["status"])

        # 3. 供应商补发
        status, ingest = self.call(
            "POST",
            "/bonds/118000/prices",
            {
                "bars": [
                    {"date": "2026-09-03", "close": "12.85", "source": "official_close"},
                    {"date": "2026-09-04", "close": "12.90", "source": "official_close"},
                    {"date": "2026-09-16", "close": "12.97", "source": "official_close"},
                ]
            },
        )
        self.assertEqual(200, status)
        self.assertEqual(3, len(ingest["accepted"]))

        # 4. 重新评估 -> 新版本
        status, j2 = self.call("POST", "/bonds/118000/evaluate", {"date": "2026-09-18"})
        self.assertEqual(201, status)
        self.assertEqual(2, j2["version"])
        call2 = next(c for c in j2["clauses"] if c["clause_id"] == "call_1")
        self.assertEqual("counting", call2["state"])
        self.assertEqual(13, call2["window"]["hits"])

        # 5. 引用被圈出
        status, refs = self.call("GET", "/references?status=stale")
        self.assertEqual(200, status)
        self.assertEqual(1, len(refs["references"]))
        self.assertEqual(j2["judgment_id"], refs["references"][0]["superseded_by"])

        # 6. 历史版本证据仍可完整取回
        status, old = self.call("GET", f"/judgments/{j1['judgment_id']}")
        self.assertEqual(200, status)
        old_call = next(c for c in old["clauses"] if c["clause_id"] == "call_1")
        self.assertEqual(15, old_call["window"]["hits"])

        # 7. 版本列表
        status, listing = self.call("GET", "/bonds/118000/judgments?valuation_date=2026-09-18")
        self.assertEqual(200, status)
        self.assertEqual([1, 2], [j["version"] for j in listing["judgments"]])

    def test_cycle_dependency_rejected_with_409(self) -> None:
        bond = {
            "bond_id": "CYC1",
            "stock_code": "600000",
            "list_date": "2026-01-05",
            "maturity_date": "2032-01-04",
            "initial_conversion_price": "10.00",
            "clauses": [
                {
                    "clause_id": "a",
                    "type": "redemption",
                    "window_days": 30,
                    "required_hits": 15,
                    "threshold_ratio": "1.30",
                    "depends_on": ["b"],
                },
                {
                    "clause_id": "b",
                    "type": "put",
                    "window_days": 30,
                    "required_hits": 30,
                    "threshold_ratio": "0.70",
                    "depends_on": ["a"],
                },
            ],
        }
        status, payload = self.call("POST", "/bonds", bond)
        self.assertEqual(409, status)
        self.assertEqual("clause_dependency_cycle", payload["error"]["code"])

    def test_validation_and_not_found(self) -> None:
        status, _ = self.call("GET", "/bonds/NOPE")
        self.assertEqual(404, status)
        status, payload = self.call("POST", "/bonds/118000/evaluate", {})
        self.assertEqual(400, status)
        self.assertEqual("missing_field", payload["error"]["code"])
        status, payload = self.call(
            "POST", "/bonds/118000/prices", {"bars": [{"date": "2026-09-21", "close": "13.00", "source": "mystery"}]}
        )
        self.assertEqual(400, status)
        status, _ = self.call("GET", "/judgments/J-does-not-exist")
        self.assertEqual(404, status)
        status, _ = self.call("GET", "/no-such-route")
        self.assertEqual(404, status)

    def test_gaps_endpoint(self) -> None:
        status, payload = self.call("GET", "/bonds/118000/gaps?date=2026-09-18")
        self.assertEqual(200, status)
        statuses = {g["status"] for g in payload["gaps"]}
        self.assertIn("missing_price", statuses)


if __name__ == "__main__":
    unittest.main()
