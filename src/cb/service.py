"""服务层：摄入、评估、自动重评估、结论引用与回放。

关键行为：
- 摄入即重评估：行情修订/补发、除权或停牌公告（含撤回）、条款更新后，
  对受影响债券的既有估值日重新评估；内容有变化才追加新判断版本；
- 旧版本永不覆盖，引用旧版本的结论在读取时被圈出（stale）并附差异摘要；
- replay 按判断的 as_of_seq 复原当时输入重算，验证证据一致性。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .clauses import normalize_terms
from .engine import evaluate_bond
from .models import (
    CORPORATE_ACTION_TYPES,
    OFFICIAL_SOURCES,
    PRICE_SOURCES,
    RECORD_STATUS,
    ValidationError,
    now_iso,
    parse_date,
    parse_money,
    require_choice,
    require_fields,
)
from .store import Store


class CbService:
    def __init__(self, store: Store, reference_dir: str | Path | None = None):
        self.store = store
        self.reference_dir = Path(reference_dir) if reference_dir else None

    # ---------- 摄入 ----------

    def ingest_terms(self, payload: dict) -> dict:
        normalized = normalize_terms(payload)
        record = self.store.append_record("terms", normalized["bond_id"], normalized)
        self._reevaluate({normalized["bond_id"]})
        return record

    def ingest_quotes(self, items: list[dict]) -> dict:
        if not isinstance(items, list) or not items:
            raise ValidationError("quotes: 需要非空数组")
        records = []
        touched_dates: list[date] = []
        symbols: set[str] = set()
        for item in items:
            require_fields(item, ["symbol", "trade_date", "close", "price_source"], "quote")
            source = require_choice(item["price_source"], PRICE_SOURCES, "quote.price_source")
            day = parse_date(item["trade_date"], "quote.trade_date")
            close = parse_money(item["close"], "quote.close")
            if close <= 0:
                raise ValidationError("quote.close: 必须为正")
            symbol = str(item["symbol"])
            payload = {
                "symbol": symbol,
                "trade_date": day.isoformat(),
                "close": str(close),
                "price_source": source,
                "business_time": day.isoformat(),
            }
            # 正式价与盘中临时价是两条独立版本线：临时价不得顶掉正式价
            lineage = "official" if source in OFFICIAL_SOURCES else "temporary"
            entity = f"{symbol}@{day.isoformat()}#{lineage}"
            records.append(self.store.append_record("quote", entity, payload))
            touched_dates.append(day)
            symbols.add(symbol)
        reeval = self._reevaluate(self._bonds_for_symbols(symbols),
                                  since=min(touched_dates))
        return {"records": records, "reevaluated": reeval}

    def ingest_corporate_action(self, payload: dict) -> dict:
        require_fields(payload, ["action_id", "symbol", "action_type", "ex_date"], "corporate_action")
        action_type = require_choice(payload["action_type"], CORPORATE_ACTION_TYPES,
                                     "corporate_action.action_type")
        status = require_choice(payload.get("status", "active"), RECORD_STATUS,
                                "corporate_action.status")
        record = self.store.append_record("corporate_action", str(payload["action_id"]), {
            "action_id": str(payload["action_id"]),
            "symbol": str(payload["symbol"]),
            "action_type": action_type,
            "params": payload.get("params", {}),
            "ex_date": parse_date(payload["ex_date"], "corporate_action.ex_date").isoformat(),
            "announcement_date": payload.get("announcement_date"),
            "status": status,
        })
        self._reevaluate(self._bonds_for_symbols({record["symbol"]}))
        return record

    def ingest_suspension(self, payload: dict) -> dict:
        require_fields(payload, ["suspension_id", "symbol", "date"], "suspension")
        status = require_choice(payload.get("status", "active"), RECORD_STATUS,
                                "suspension.status")
        record = self.store.append_record("suspension", str(payload["suspension_id"]), {
            "suspension_id": str(payload["suspension_id"]),
            "symbol": str(payload["symbol"]),
            "date": parse_date(payload["date"], "suspension.date").isoformat(),
            "reason": payload.get("reason"),
            "status": status,
        })
        self._reevaluate(self._bonds_for_symbols({record["symbol"]}))
        return record

    def ingest_price_reset(self, payload: dict) -> dict:
        require_fields(payload, ["reset_id", "bond_id", "effective_date", "new_price"],
                       "price_reset")
        status = require_choice(payload.get("status", "active"), RECORD_STATUS,
                                "price_reset.status")
        new_price = parse_money(payload["new_price"], "price_reset.new_price")
        if new_price <= 0:
            raise ValidationError("price_reset.new_price: 必须为正")
        record = self.store.append_record("price_reset", str(payload["reset_id"]), {
            "reset_id": str(payload["reset_id"]),
            "bond_id": str(payload["bond_id"]),
            "effective_date": parse_date(payload["effective_date"],
                                         "price_reset.effective_date").isoformat(),
            "new_price": str(new_price),
            "status": status,
            "note": payload.get("note"),
        })
        self._reevaluate({record["bond_id"]})
        return record

    def ingest_calendar(self, payload: dict) -> dict:
        require_fields(payload, ["calendar_id", "trading_days"], "calendar")
        days = sorted({parse_date(d, "calendar.trading_days").isoformat()
                       for d in payload["trading_days"]})
        return self.store.append_record("calendar", str(payload["calendar_id"]), {
            "calendar_id": str(payload["calendar_id"]),
            "trading_days": days,
        })

    # ---------- 评估 ----------

    def evaluate(self, bond_id: str, valuation_date: str, persist: bool = True) -> dict:
        valuation = parse_date(valuation_date, "valuation_date")
        content = evaluate_bond(self.store, bond_id, valuation)
        latest = self.store.latest_judgment(bond_id, content["valuation_date"])
        if latest and latest["content_hash"] == content["content_hash"]:
            return {"judgment": latest, "created": False}
        judgment = {
            **content,
            "judgment_id": f"{bond_id}:{content['valuation_date']}:v"
                           f"{(latest['version_no'] + 1) if latest else 1}",
            "version_no": (latest["version_no"] + 1) if latest else 1,
            "created_at": now_iso(),
            "supersedes": latest["judgment_id"] if latest else None,
        }
        if persist:
            self.store.append_judgment(judgment)
        return {"judgment": judgment, "created": True}

    def _reevaluate(self, bond_ids: set[str], since: date | None = None) -> list[dict]:
        """对受影响债券的既有估值日重评估；内容变化才追加新版本。"""
        created = []
        for bond_id in sorted(bond_ids):
            dates = sorted({j["valuation_date"] for j in self.store.judgments(bond_id)})
            for vdate in dates:
                if since is not None and parse_date(vdate, "valuation_date") < since:
                    continue
                result = self.evaluate(bond_id, vdate)
                if result["created"]:
                    created.append({
                        "bond_id": bond_id,
                        "valuation_date": vdate,
                        "judgment_id": result["judgment"]["judgment_id"],
                        "version_no": result["judgment"]["version_no"],
                    })
        return created

    def _bonds_for_symbols(self, symbols: set[str]) -> set[str]:
        out = set()
        for rec in self.store.state_as_of(kind="terms"):
            if rec["underlying"] in symbols:
                out.add(rec["bond_id"])
        return out

    # ---------- 引用与回放 ----------

    def cite(self, judgment_id: str, cited_by: str, note: str | None = None) -> dict:
        judgment = self.store.get_judgment(judgment_id)
        if judgment is None:
            raise ValidationError(f"判断版本不存在: {judgment_id}")
        return self.store.append_reference({
            "judgment_id": judgment_id,
            "bond_id": judgment["bond_id"],
            "valuation_date": judgment["valuation_date"],
            "cited_by": cited_by,
            "cited_at": now_iso(),
            "note": note,
        })

    def references(self, bond_id: str) -> list[dict]:
        out = []
        for ref in self.store.references(bond_id):
            latest = self.store.latest_judgment(ref["bond_id"], ref["valuation_date"])
            stale = latest is not None and latest["judgment_id"] != ref["judgment_id"]
            entry = {**ref, "stale": stale,
                     "current_judgment_id": latest["judgment_id"] if latest else None}
            if stale:
                old = self.store.get_judgment(ref["judgment_id"])
                entry["diff_summary"] = diff_judgments(old, latest) if old else []
            out.append(entry)
        return out

    def replay(self, judgment_id: str) -> dict:
        judgment = self.store.get_judgment(judgment_id)
        if judgment is None:
            raise ValidationError(f"判断版本不存在: {judgment_id}")
        recomputed = evaluate_bond(
            self.store,
            judgment["bond_id"],
            parse_date(judgment["valuation_date"], "valuation_date"),
            as_of_seq=judgment["manifest"]["as_of_seq"],
        )
        matches = recomputed["content_hash"] == judgment["content_hash"]
        return {
            "judgment_id": judgment_id,
            "matches": matches,
            "stored_hash": judgment["content_hash"],
            "recomputed_hash": recomputed["content_hash"],
            "evidence": judgment if matches else recomputed,
        }

    # ---------- 样例数据 ----------

    def seed(self) -> dict:
        if self.reference_dir is None:
            raise ValidationError("未配置 reference 目录")
        if not self.store.is_empty():
            raise ValidationError("存储非空，seed 仅在空库上执行")
        loaded: dict[str, int] = {}
        calendar_path = self.reference_dir / "calendar.sample.json"
        if calendar_path.exists():
            self.ingest_calendar(json.loads(calendar_path.read_text(encoding="utf-8")))
            loaded["calendar"] = 1
        terms_path = self.reference_dir / "bond_terms.sample.json"
        # 条款文件可以是单个对象或数组
        if terms_path.exists():
            terms_payload = json.loads(terms_path.read_text(encoding="utf-8"))
            items = terms_payload if isinstance(terms_payload, list) else [terms_payload]
            for item in items:
                self.ingest_terms(item)
            loaded["terms"] = len(items)
        for name, ingester, key in (
            ("quotes.sample.json", self.ingest_quotes, "quotes"),
            ("corporate_actions.sample.json", None, "corporate_actions"),
            ("suspensions.sample.json", None, "suspensions"),
            ("price_resets.sample.json", None, "price_resets"),
        ):
            path = self.reference_dir / name
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            items = payload if isinstance(payload, list) else [payload]
            if key == "quotes":
                ingester(items)
            elif key == "corporate_actions":
                for item in items:
                    self.ingest_corporate_action(item)
            elif key == "suspensions":
                for item in items:
                    self.ingest_suspension(item)
            else:
                for item in items:
                    self.ingest_price_reset(item)
            loaded[key] = len(items)
        return loaded


def diff_judgments(old: dict, new: dict) -> list[str]:
    """两个判断版本之间的结论级差异摘要（用于圈出旧引用）。"""
    diffs: list[str] = []
    old_price = old["effective_terms"]["conversion_price_at_valuation"]
    new_price = new["effective_terms"]["conversion_price_at_valuation"]
    if old_price != new_price:
        diffs.append(f"转股价 {old_price} → {new_price}")
    for cid in new["dependency_order"]:
        before = old["clauses"].get(cid)
        after = new["clauses"].get(cid)
        if before is None or after is None:
            diffs.append(f"{cid}: 条款集合变化")
            continue
        if before.get("status") != after.get("status"):
            diffs.append(f"{cid}: 状态 {before.get('status')} → {after.get('status')}")
        old_t = (before.get("trigger") or {}).get("trigger_date")
        new_t = (after.get("trigger") or {}).get("trigger_date")
        if old_t != new_t:
            diffs.append(f"{cid}: 触发日 {old_t or '—'} → {new_t or '—'}")
        old_s = before.get("summary") or {}
        new_s = after.get("summary") or {}
        for key, label in (("hits_in_current_window", "窗口内达标天数"),
                           ("applicable_days", "适用交易日数"),
                           ("needed_to_trigger", "距触发尚缺天数")):
            if old_s.get(key) != new_s.get(key):
                diffs.append(f"{cid}: {label} {old_s.get(key)} → {new_s.get(key)}")
        old_gaps = len(before.get("data_gaps", []))
        new_gaps = len(after.get("data_gaps", []))
        if old_gaps != new_gaps:
            diffs.append(f"{cid}: 数据缺口 {old_gaps} → {new_gaps}")
    return diffs
