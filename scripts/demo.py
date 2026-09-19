#!/usr/bin/env python3
"""晨会场景演示：行情补发如何产生新判断版本并圈出旧引用。

用法：python3 scripts/demo.py
在临时目录中运行，不污染 .runtime/。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cb.service import CbService  # noqa: E402
from cb.store import Store  # noqa: E402

BOND = "CB_DEMO"
VDATE = "2026-09-18"


def show_call(judgment: dict) -> None:
    call = judgment["clauses"]["call_130"]
    s = call["summary"]
    print(f"  [{judgment['judgment_id']}] 强赎条款 call_130: {call['status']}, "
          f"窗口内达标 {s['hits_in_current_window']}/15, 尚缺 {s['needed_to_trigger']} 天")
    proj = call.get("projection") or {}
    if proj.get("earliest_trigger_date"):
        print(f"    若未来每日达标，最早触发日 {proj['earliest_trigger_date']}，"
          f"需达标日期 {proj['must_hit_dates']}")
    for gap in call["data_gaps"]:
        print(f"    数据缺口 {gap['date']}: {gap['reason']} — {gap['detail']}")


def main() -> None:
    svc = CbService(Store(tempfile.mkdtemp()), ROOT / "reference")
    svc.seed()
    print("== 1. 晨会前评估（09-10/09-11 收盘价未到，09-18 仅盘中临时价）==")
    j1 = svc.evaluate(BOND, VDATE)["judgment"]
    show_call(j1)

    print("\n== 2. 客户经理引用该结论 ==")
    ref = svc.cite(j1["judgment_id"], "客户经理A", "晨会PPT引用")
    print(f"  引用 {ref['reference_id']} -> {ref['judgment_id']}")

    print("\n== 3. 行情供应商补发 09-10/09-11 收盘价 ==")
    result = svc.ingest_quotes([
        {"symbol": "600000", "trade_date": "2026-09-10",
         "close": "12.90", "price_source": "corrected"},
        {"symbol": "600000", "trade_date": "2026-09-11",
         "close": "12.95", "price_source": "corrected"},
    ])
    for r in result["reevaluated"]:
        print(f"  自动生成新判断版本 {r['judgment_id']}")
    show_call(svc.store.latest_judgment(BOND, VDATE))

    print("\n== 4. 曾引用旧版本的结论被圈出（旧版本未被覆盖）==")
    for r in svc.references(BOND):
        print(f"  {r['reference_id']} stale={r['stale']} 当前版本 {r['current_judgment_id']}")
        for d in r.get("diff_summary", []):
            print(f"    差异: {d}")

    print("\n== 5. 09-18 正式收盘价到达，盘中临时价被区分处理 ==")
    svc.ingest_quotes([
        {"symbol": "600000", "trade_date": "2026-09-18",
         "close": "12.72", "price_source": "official_close"},
    ])
    show_call(svc.store.latest_judgment(BOND, VDATE))

    print("\n== 6. 逐日复核：回放最初版本，证据与当时完全一致 ==")
    replay = svc.replay(j1["judgment_id"])
    print(f"  回放 {replay['judgment_id']}: matches={replay['matches']} "
          f"hash={replay['stored_hash']}")
    versions = [j["judgment_id"] for j in svc.store.judgments(BOND, VDATE)]
    print(f"  全部版本保留: {versions}")


if __name__ == "__main__":
    main()
