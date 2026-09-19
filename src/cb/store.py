"""追加式版本化存储。

所有业务记录（条款、行情、除权、停牌、下修、交易日历）都是不可变的：
每次修订追加一条新记录，带递增的 record_version（实体级）和 ingest_seq（全局级）。
判断版本与引用同样只追加。任何历史状态都可以通过 ingest_seq 精确复原，
因此"不得悄悄覆盖"由存储结构保证，而不是靠调用方自觉。
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import now_iso

RECORD_KINDS = (
    "terms",
    "calendar",
    "quote",
    "corporate_action",
    "suspension",
    "price_reset",
)


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._records: list[dict] = []
        self._judgments: list[dict] = []
        self._references: list[dict] = []
        self._seq = 0
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        for name, target in (
            ("records.jsonl", self._records),
            ("judgments.jsonl", self._judgments),
            ("references.jsonl", self._references),
        ):
            path = self.root / name
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        target.append(json.loads(line))
        seqs = [r["ingest_seq"] for r in self._records]
        seqs += [j["ingest_seq"] for j in self._judgments]
        seqs += [r["ingest_seq"] for r in self._references]
        self._seq = max(seqs, default=0)

    def _append(self, filename: str, payload: dict) -> None:
        with (self.root / filename).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            fh.write("\n")

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ---------- 业务记录 ----------

    def append_record(self, kind: str, entity_id: str, payload: dict) -> dict:
        if kind not in RECORD_KINDS:
            raise ValueError(f"unknown record kind: {kind}")
        version = 1 + max(
            (r["record_version"] for r in self._records
             if r["kind"] == kind and r["entity_id"] == entity_id),
            default=0,
        )
        record = {
            "record_id": f"{kind}:{entity_id}:v{version}",
            "kind": kind,
            "entity_id": entity_id,
            "record_version": version,
            "ingest_seq": self._next_seq(),
            "ingested_at": now_iso(),
            **payload,
        }
        self._records.append(record)
        self._append("records.jsonl", record)
        return record

    def records(self, kind: str | None = None, entity_id: str | None = None) -> list[dict]:
        out = self._records
        if kind is not None:
            out = [r for r in out if r["kind"] == kind]
        if entity_id is not None:
            out = [r for r in out if r["entity_id"] == entity_id]
        return list(out)

    def state_as_of(self, seq: int | None = None, kind: str | None = None) -> list[dict]:
        """每个 (kind, entity_id) 在指定 ingest_seq 时点的最新版本（None 表示当前）。"""
        state: dict[tuple[str, str], dict] = {}
        for rec in self._records:
            if kind is not None and rec["kind"] != kind:
                continue
            if seq is not None and rec["ingest_seq"] > seq:
                continue
            key = (rec["kind"], rec["entity_id"])
            if key not in state or rec["ingest_seq"] > state[key]["ingest_seq"]:
                state[key] = rec
        return list(state.values())

    def get_record(self, record_id: str) -> dict | None:
        for rec in self._records:
            if rec["record_id"] == record_id:
                return rec
        return None

    # ---------- 判断版本 ----------

    def append_judgment(self, judgment: dict) -> dict:
        judgment = {**judgment, "ingest_seq": self._next_seq()}
        self._judgments.append(judgment)
        self._append("judgments.jsonl", judgment)
        return judgment

    def judgments(self, bond_id: str | None = None, valuation_date: str | None = None) -> list[dict]:
        out = self._judgments
        if bond_id is not None:
            out = [j for j in out if j["bond_id"] == bond_id]
        if valuation_date is not None:
            out = [j for j in out if j["valuation_date"] == valuation_date]
        return list(out)

    def get_judgment(self, judgment_id: str) -> dict | None:
        for j in self._judgments:
            if j["judgment_id"] == judgment_id:
                return j
        return None

    def latest_judgment(self, bond_id: str, valuation_date: str) -> dict | None:
        versions = self.judgments(bond_id, valuation_date)
        return max(versions, key=lambda j: j["version_no"], default=None)

    # ---------- 结论引用 ----------

    def append_reference(self, reference: dict) -> dict:
        reference = {
            "reference_id": f"R{len(self._references) + 1:04d}",
            "ingest_seq": self._next_seq(),
            **reference,
        }
        self._references.append(reference)
        self._append("references.jsonl", reference)
        return reference

    def references(self, bond_id: str | None = None) -> list[dict]:
        out = self._references
        if bond_id is not None:
            out = [r for r in out if r["bond_id"] == bond_id]
        return list(out)

    # ---------- 其他 ----------

    def is_empty(self) -> bool:
        return not self._records

    @property
    def current_seq(self) -> int:
        return self._seq

    @property
    def latest_record_seq(self) -> int:
        """业务记录（不含判断与引用）的最大 ingest_seq，作为评估的输入游标。"""
        return max((r["ingest_seq"] for r in self._records), default=0)
