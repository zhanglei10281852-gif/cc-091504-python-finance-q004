#!/usr/bin/env python3
"""晨会情景演示：行情补发如何改变强赎判断，以及旧引用如何被圈出。

流程：
  1. 研究员基于基础行情评估 2026-09-18 -> 判断版本 v1（强赎条件满足，但有数据缺口）；
  2. 客户经理登记对 v1 的引用；
  3. 行情供应商补发两个交易日收盘价并修订一日价格；
  4. 重新评估 -> 判断版本 v2（达标天数 15 -> 13，条件不再满足）；
  5. 系统圈出引用过 v1 的结论并附差异；v1 的完整证据仍可复原。

运行：python3 scripts/demo_morning_meeting.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import create_server  # noqa: E402
from cb.seed import seed_if_empty  # noqa: E402
from cb.store import Store  # noqa: E402


def call(port: int, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method
    )
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def clause(judgment: dict, clause_id: str) -> dict:
    return next(c for c in judgment["clauses"] if c["clause_id"] == clause_id)


def brief(judgment: dict) -> str:
    call1 = clause(judgment, "call_1")
    window = call1["window"]
    threshold = call1["days"][0]["threshold"] if call1.get("days") else "-"
    lines = [
        f"  判断版本 {judgment['judgment_id']}（数据版本 v{judgment['data_version']}）",
        f"  有效转股价 {judgment['effective_conversion_price']}，强赎阈值 130% = {threshold}",
        f"  强赎窗口 {window['window_start']} ~ {window['window_end']}："
        f"计入 {window['counted_days']} 个适用交易日，达标 {window['hits']} / 要求 {window['required_hits']}",
        f"  状态: {call1['state']}",
    ]
    remaining = call1["remaining"]
    if remaining["hits_needed"]:
        lines.append(
            f"  距触发还差 {remaining['hits_needed']} 天达标，"
            f"最早可能触发日 {remaining['earliest_trigger_date']}（{', '.join(remaining['needed_dates'])}）"
        )
    if call1["gaps"]:
        lines.append(
            "  数据缺口: " + ", ".join(f"{g['date']}({g['status']})" for g in call1["gaps"])
        )
    excluded = call1["excluded_days"]
    if excluded:
        lines.append(
            "  排除日: " + ", ".join(f"{d['date']}({d['status']})" for d in excluded)
        )
    return "\n".join(lines)


def main() -> None:
    tmp = tempfile.TemporaryDirectory()
    store = Store(tmp.name)  # 演示用独立存储，不污染 .runtime/
    seed_if_empty(store, ROOT / "reference")
    server = create_server("127.0.0.1", 0, store=store)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        print("=" * 72)
        print("第一步：研究员在晨会前评估 2026-09-18")
        print("=" * 72)
        j1 = call(port, "POST", "/bonds/118000/evaluate", {"date": "2026-09-18", "note": "晨会复核"})
        print(brief(j1))

        print()
        print("第二步：客户经理引用该结论")
        ref = call(
            port,
            "POST",
            "/references",
            {"judgment_id": j1["judgment_id"], "consumer": "客户经理-王某", "note": "晨会引用"},
        )
        print(f"  引用 {ref['reference_id']} -> {ref['judgment_id']} 状态 {ref['status']}")

        print()
        print("第三步：行情供应商补发 9/3、9/4 收盘价，修订 9/15，补齐 9/16 官方价")
        correction = json.loads((ROOT / "reference/price_correction_20260919.json").read_text("utf-8"))
        ingest = call(port, "POST", "/bonds/118000/prices", correction)
        print(f"  数据版本推进到 v{ingest['data_version']}，取代 {len(ingest['superseded'])} 条旧记录")

        print()
        print("第四步：基于新数据重新评估同一估值日")
        j2 = call(port, "POST", "/bonds/118000/evaluate", {"date": "2026-09-18", "note": "补发后复核"})
        print(brief(j2))
        if j2["changes_from_previous"]:
            print("  与上一版本差异:")
            for line in j2["changes_from_previous"]:
                print(f"    - {line}")

        print()
        print("第五步：曾引用旧版本的结论被圈出（不覆盖历史）")
        stale = call(port, "GET", "/references?status=stale")
        for item in stale["references"]:
            print(f"  {item['reference_id']} {item['consumer']}: {item['judgment_id']} -> {item['superseded_by']}")
            for line in item["diff"]:
                print(f"    差异: {line}")

        print()
        print("第六步：复原 v1 当时看到的完整证据（前 3 个计入日示例）")
        old = call(port, "GET", f"/judgments/{j1['judgment_id']}")
        old_call = clause(old, "call_1")
        for day in old_call["days"][:3]:
            print(
                f"    {day['date']}: 收盘 {day['close']} vs 阈值 {day['threshold']} "
                f"({day['comparison']}) -> {'达标' if day['hit'] else '未达标'} "
                f"[{day['price_source']} r{day['price_revision']}]"
            )
        print(f"  v1 证据哈希: {old['evidence_hash'][:16]}...  达标 {old_call['window']['hits']} 天（当时结论）")
    finally:
        server.shutdown()
        server.server_close()
        tmp.cleanup()


if __name__ == "__main__":
    main()
