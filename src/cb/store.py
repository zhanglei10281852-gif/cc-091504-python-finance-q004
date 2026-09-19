"""持久化：JSON 文件存储，写入 .runtime/。

- 每次写操作先写临时文件再原子替换，避免半截文件；
- data_version 是全局单调递增计数器，任何行情/事件/条款变更都会推进它，
  判断版本据此标记自己是否基于最新数据；
- 所有集合都只追加或状态翻转（active -> superseded/retracted），不做物理删除。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .calendar import TradingCalendar
from .models import Bond, Event, PriceBar
from .util import canonical_json


class Store:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "prices").mkdir(exist_ok=True)
        (self.data_dir / "events").mkdir(exist_ok=True)
        (self.data_dir / "judgments").mkdir(exist_ok=True)
        self._meta: dict[str, Any] = self._load("meta.json", {"data_version": 0})
        self._calendars: dict[str, dict] = self._load("calendars.json", {})
        self._bonds: dict[str, dict] = self._load("bonds.json", {})
        self._references: list[dict] = self._load("references.json", [])

    # ---------- 基础读写 ----------

    def _path(self, name: str) -> Path:
        return self.data_dir / name

    def _load(self, name: str, default: Any) -> Any:
        path = self._path(name)
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _atomic_write(self, name: str, payload: Any) -> None:
        path = self._path(name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)

    # ---------- 数据版本 ----------

    @property
    def data_version(self) -> int:
        return int(self._meta.get("data_version", 0))

    def bump_data_version(self) -> int:
        self._meta["data_version"] = self.data_version + 1
        self._atomic_write("meta.json", self._meta)
        return self.data_version

    # ---------- 交易日历 ----------

    def save_calendar(self, calendar: TradingCalendar) -> None:
        self._calendars[calendar.calendar_id] = calendar.to_dict()
        self._atomic_write("calendars.json", self._calendars)

    def get_calendar(self, calendar_id: str) -> TradingCalendar | None:
        raw = self._calendars.get(calendar_id)
        return TradingCalendar.from_dict(raw) if raw else None

    # ---------- 债券 ----------

    def save_bond(self, bond: Bond) -> None:
        self._bonds[bond.bond_id] = bond.to_dict()
        self._atomic_write("bonds.json", self._bonds)

    def get_bond(self, bond_id: str) -> Bond | None:
        raw = self._bonds.get(bond_id)
        return Bond.from_dict(raw) if raw else None

    def list_bonds(self) -> list[Bond]:
        return [Bond.from_dict(raw) for raw in self._bonds.values()]

    def has_bonds(self) -> bool:
        return bool(self._bonds)

    # ---------- 行情（按正股代码） ----------

    def get_prices(self, stock_code: str) -> list[PriceBar]:
        raw = self._load(f"prices/{stock_code}.json", [])
        return [PriceBar.from_dict(item) for item in raw]

    def save_prices(self, stock_code: str, bars: list[PriceBar]) -> None:
        self._atomic_write(f"prices/{stock_code}.json", [b.to_dict() for b in bars])

    # ---------- 事件（按债券） ----------

    def get_events(self, bond_id: str) -> list[Event]:
        raw = self._load(f"events/{bond_id}.json", [])
        return [Event.from_dict(item) for item in raw]

    def save_events(self, bond_id: str, events: list[Event]) -> None:
        self._atomic_write(f"events/{bond_id}.json", [e.to_dict() for e in events])

    # ---------- 判断版本 ----------

    def save_judgment(self, judgment: dict[str, Any]) -> None:
        bond_id = judgment["bond_id"]
        items = self._load(f"judgments/{bond_id}.json", [])
        items.append(judgment)
        self._atomic_write(f"judgments/{bond_id}.json", items)

    def judgments_for(self, bond_id: str, valuation_date: str | None = None) -> list[dict]:
        items: list[dict] = self._load(f"judgments/{bond_id}.json", [])
        if valuation_date is not None:
            items = [j for j in items if j.get("valuation_date") == valuation_date]
        return sorted(items, key=lambda j: j.get("version", 0))

    def latest_judgment(self, bond_id: str, valuation_date: str) -> dict | None:
        items = self.judgments_for(bond_id, valuation_date)
        return items[-1] if items else None

    def get_judgment(self, judgment_id: str) -> dict | None:
        for bond_id in self._bonds:
            for j in self._load(f"judgments/{bond_id}.json", []):
                if j.get("judgment_id") == judgment_id:
                    return j
        return None

    # ---------- 外部引用 ----------

    def add_reference(self, reference: dict[str, Any]) -> None:
        self._references.append(reference)
        self._atomic_write("references.json", self._references)

    def list_references(self, status: str | None = None) -> list[dict]:
        refs = self._references
        if status:
            refs = [r for r in refs if r.get("status") == status]
        return list(refs)

    def update_reference(self, reference_id: str, **changes: Any) -> None:
        for ref in self._references:
            if ref.get("reference_id") == reference_id:
                ref.update(changes)
        self._atomic_write("references.json", self._references)

    # ---------- 调试 ----------

    def digest(self) -> str:
        return canonical_json(
            {
                "data_version": self.data_version,
                "bonds": sorted(self._bonds),
                "references": len(self._references),
            }
        )
