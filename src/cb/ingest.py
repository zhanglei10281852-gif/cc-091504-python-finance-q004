"""数据接入：行情与事件的版本化写入。

- 官方收盘/修订价：同一日期新记录使旧的 active 记录转为 superseded（保留可查）；
- 盘中临时价：只存不用，绝不参与正式判断，也不取代官方价；
- action=retract：撤回某日全部行情（供应商撤价），该日转为缺价；
- 事件：retraction 类型把目标事件置为 retracted（公告撤回），事件本身保留；
- 每次接入都会推进 data_version，并在返回摘要中列出被取代/撤回的记录。
"""

from __future__ import annotations

from typing import Any

from .models import (
    BAR_ACTIVE,
    BAR_RETRACTED,
    BAR_SUPERSEDED,
    EVENT_ACTIVE,
    EVENT_RETRACTED,
    EVENT_TYPES,
    PRICE_SOURCES,
    Event,
    PriceBar,
)
from .store import Store
from .util import dec, parse_date, utcnow_iso


class IngestError(ValueError):
    pass


def ingest_prices(
    store: Store, stock_code: str, bars: list[dict[str, Any]], received_at: str | None = None
) -> dict[str, Any]:
    if not bars:
        raise IngestError("bars 不能为空")
    received_at = received_at or utcnow_iso()
    existing = store.get_prices(stock_code)
    superseded: list[dict] = []
    retracted: list[dict] = []
    accepted: list[dict] = []

    for item in bars:
        bar_date = parse_date(item["date"])
        action = item.get("action")
        if action == "retract":
            hit = False
            for old in existing:
                if old.bar_date == bar_date and old.status == BAR_ACTIVE:
                    old.status = BAR_RETRACTED
                    retracted.append(old.to_dict())
                    hit = True
            if not hit:
                raise IngestError(f"{bar_date} 无可撤回的行情记录")
            continue

        source = item.get("source")
        if source not in PRICE_SOURCES:
            raise IngestError(f"未知行情来源: {source}")
        if "close" not in item:
            raise IngestError(f"{bar_date} 缺少收盘价")
        close = dec(item["close"])
        if close <= 0:
            raise IngestError(f"{bar_date} 收盘价必须为正数")

        revision = 1 + max((b.revision for b in existing if b.bar_date == bar_date), default=0)
        if source in ("official_close", "corrected"):
            for old in existing:
                if (
                    old.bar_date == bar_date
                    and old.status == BAR_ACTIVE
                    and old.source in ("official_close", "corrected")
                ):
                    old.status = BAR_SUPERSEDED
                    superseded.append(old.to_dict())
        else:  # temporary：只取代更早的临时价，不触碰官方价
            for old in existing:
                if old.bar_date == bar_date and old.status == BAR_ACTIVE and old.source == "temporary":
                    old.status = BAR_SUPERSEDED
                    superseded.append(old.to_dict())

        bar = PriceBar(
            stock_code=stock_code,
            bar_date=bar_date,
            close=close,
            source=source,
            revision=revision,
            received_at=received_at,
            status=BAR_ACTIVE,
            bar_id=f"{stock_code}:{bar_date.isoformat()}:{source}:r{revision}",
        )
        existing.append(bar)
        accepted.append(bar.to_dict())

    store.save_prices(stock_code, existing)
    data_version = store.bump_data_version()
    return {
        "stock_code": stock_code,
        "data_version": data_version,
        "accepted": accepted,
        "superseded": superseded,
        "retracted": retracted,
    }


def ingest_events(
    store: Store, bond_id: str, events: list[dict[str, Any]], received_at: str | None = None
) -> dict[str, Any]:
    if not events:
        raise IngestError("events 不能为空")
    if store.get_bond(bond_id) is None:
        raise IngestError(f"债券不存在: {bond_id}")
    received_at = received_at or utcnow_iso()
    existing = store.get_events(bond_id)
    known_ids = {e.event_id for e in existing}
    accepted: list[dict] = []
    retracted_events: list[dict] = []

    for item in events:
        event_type = item.get("event_type")
        if event_type not in EVENT_TYPES:
            raise IngestError(f"未知事件类型: {event_type}")
        params = dict(item.get("params", {}))
        if event_type == "retraction":
            target_id = params.get("target_event_id")
            target = next((e for e in existing if e.event_id == target_id), None)
            if target is None:
                raise IngestError(f"撤回的目标事件不存在: {target_id}")
            if target.status != EVENT_ACTIVE:
                raise IngestError(f"事件 {target_id} 当前状态不可撤回: {target.status}")
            target.status = EVENT_RETRACTED
            retracted_events.append(target.to_dict())

        event_id = item.get("event_id") or f"EV-{bond_id}-{len(existing) + 1:04d}"
        if event_id in known_ids:
            raise IngestError(f"event_id 重复: {event_id}")
        known_ids.add(event_id)
        eff = item.get("effective_date")
        event = Event(
            event_id=event_id,
            bond_id=bond_id,
            event_type=event_type,
            params=params,
            received_at=item.get("received_at", received_at),
            effective_date=parse_date(eff) if eff else None,
            status=EVENT_ACTIVE,
        )
        existing.append(event)
        accepted.append(event.to_dict())

    store.save_events(bond_id, existing)
    data_version = store.bump_data_version()
    return {
        "bond_id": bond_id,
        "data_version": data_version,
        "accepted": accepted,
        "retracted_events": retracted_events,
    }
