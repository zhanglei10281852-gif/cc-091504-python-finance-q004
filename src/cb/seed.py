"""样例数据装载：把 reference/ 下的条款、行情、事件、交易日历样例装入存储。

仅在存储为空时执行（服务首次启动），保证演示与晨会复核有一致的起点；
后续的行情补发、公告撤回都通过 API 接入，形成新的数据版本。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .calendar import TradingCalendar
from .ingest import ingest_events, ingest_prices
from .models import Bond
from .store import Store


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def seed_if_empty(store: Store, reference_dir: str | Path) -> dict[str, Any]:
    if store.has_bonds():
        return {"seeded": False, "reason": "存储非空，跳过样例装载"}
    ref = Path(reference_dir)
    summary: dict[str, Any] = {"seeded": True, "bonds": [], "calendars": []}

    calendar_path = ref / "calendar_cn_equity.json"
    if calendar_path.exists():
        calendar = TradingCalendar.from_dict(_load_json(calendar_path))
        store.save_calendar(calendar)
        summary["calendars"].append(calendar.calendar_id)

    for bond_path in sorted(ref.glob("bond_*.json")):
        bond = Bond.from_dict(_load_json(bond_path))
        store.save_bond(bond)
        summary["bonds"].append(bond.bond_id)

        events_path = ref / f"events_{bond.bond_id}.json"
        if events_path.exists():
            payload = _load_json(events_path)
            ingest_events(store, bond.bond_id, payload.get("events", payload))

        prices_path = ref / f"prices_{bond.stock_code}_base.json"
        if prices_path.exists():
            payload = _load_json(prices_path)
            ingest_prices(
                store,
                bond.stock_code,
                payload["bars"],
                received_at=payload.get("received_at"),
            )

    summary["data_version"] = store.data_version
    return summary
