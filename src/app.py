"""HTTP 接口层：标准库实现，JSON 响应，持久化写入 .runtime/。

路由一览：
  GET  /health
  POST /admin/seed                         装载 reference/ 样例（存储为空时）
  GET  /bonds                              债券列表
  POST /bonds                              登记债券与条款（循环依赖返回 409）
  GET  /bonds/{bond_id}                    债券详情 + 判断版本摘要
  POST /bonds/{bond_id}/prices             接入行情（官方/修订/临时/撤回）
  POST /bonds/{bond_id}/events             接入事件（除权/下修/停牌/公告撤回）
  POST /bonds/{bond_id}/evaluate           按估值日评估，生成新判断版本
  GET  /bonds/{bond_id}/judgments          判断版本列表（?valuation_date=）
  GET  /bonds/{bond_id}/gaps               数据缺口扫描（?date=）
  GET  /judgments/{judgment_id}            任一历史判断版本的完整证据
  POST /references                         登记外部结论对某判断版本的引用
  GET  /references                         引用列表（?status=stale 看被圈出的）
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from cb.dependencies import ClauseDependencyError, evaluation_order
from cb.engine import EvaluationError, NotFoundError, add_reference, evaluate, scan_gaps
from cb.ingest import IngestError, ingest_events, ingest_prices
from cb.models import Bond
from cb.seed import seed_if_empty
from cb.store import Store

SERVICE_NAME = "可转债条款分析服务"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE_DIR = ROOT / "reference"

WINDOW_CLAUSE_TYPES = ("redemption", "put", "conversion_price_reset")
KNOWN_CLAUSE_TYPES = WINDOW_CLAUSE_TYPES + ("maturity",)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


# ---------------------------------------------------------------- 输入校验


def _require(data: dict[str, Any], field: str) -> Any:
    value = data.get(field)
    if value is None or value == "":
        raise ApiError(400, "missing_field", f"缺少字段: {field}")
    return value


def _validate_bond_payload(data: dict[str, Any]) -> Bond:
    for field in ("bond_id", "stock_code", "list_date", "maturity_date", "initial_conversion_price"):
        _require(data, field)
    clauses = data.get("clauses", [])
    if not isinstance(clauses, list):
        raise ApiError(400, "invalid_clauses", "clauses 必须是数组")
    for clause in clauses:
        ctype = clause.get("type")
        if ctype not in KNOWN_CLAUSE_TYPES:
            raise ApiError(400, "invalid_clause", f"未知条款类型: {ctype}")
        if not clause.get("clause_id"):
            raise ApiError(400, "invalid_clause", "条款缺少 clause_id")
        if ctype in WINDOW_CLAUSE_TYPES:
            for field in ("window_days", "required_hits", "threshold_ratio"):
                if clause.get(field) is None:
                    raise ApiError(400, "invalid_clause", f"{clause['clause_id']} 缺少 {field}")
    try:
        bond = Bond.from_dict(data)
    except Exception as exc:  # 日期/数字格式问题
        raise ApiError(400, "invalid_bond", f"债券定义无法解析: {exc}") from exc
    if bond.maturity_date <= bond.list_date:
        raise ApiError(400, "invalid_bond", "到期日必须晚于上市日")
    try:
        evaluation_order(bond.clauses)
    except ClauseDependencyError as exc:
        raise ApiError(409, "clause_dependency_cycle", str(exc)) from exc
    return bond


# ---------------------------------------------------------------- 路由处理


def _bond_or_404(store: Store, bond_id: str) -> Bond:
    bond = store.get_bond(bond_id)
    if bond is None:
        raise ApiError(404, "bond_not_found", f"债券不存在: {bond_id}")
    return bond


def handle_list_bonds(store: Store, **_: Any) -> tuple[int, Any]:
    return 200, {"bonds": [b.to_dict() for b in store.list_bonds()]}


def handle_create_bond(store: Store, body: dict, **_: Any) -> tuple[int, Any]:
    bond = _validate_bond_payload(body)
    if store.get_bond(bond.bond_id) is not None:
        raise ApiError(409, "bond_exists", f"债券已存在: {bond.bond_id}")
    if store.get_calendar(bond.calendar_id) is None:
        raise ApiError(400, "calendar_missing", f"交易日历未登记: {bond.calendar_id}")
    store.save_bond(bond)
    store.bump_data_version()
    return 201, bond.to_dict()


def handle_get_bond(store: Store, match: re.Match, **_: Any) -> tuple[int, Any]:
    bond_id = match.group("bond_id")
    bond = _bond_or_404(store, bond_id)
    judgments = [
        {
            "judgment_id": j["judgment_id"],
            "valuation_date": j["valuation_date"],
            "version": j["version"],
            "data_version": j["data_version"],
            "data_stale": j["data_version"] < store.data_version,
        }
        for j in store.judgments_for(bond_id)
    ]
    payload = bond.to_dict()
    payload["data_version"] = store.data_version
    payload["judgments"] = judgments
    return 200, payload


def handle_ingest_prices(store: Store, match: re.Match, body: dict, **_: Any) -> tuple[int, Any]:
    bond = _bond_or_404(store, match.group("bond_id"))
    bars = body.get("bars")
    if not isinstance(bars, list):
        raise ApiError(400, "invalid_body", "bars 必须是数组")
    try:
        result = ingest_prices(store, bond.stock_code, bars, received_at=body.get("received_at"))
    except IngestError as exc:
        raise ApiError(400, "ingest_error", str(exc)) from exc
    return 200, result


def handle_ingest_events(store: Store, match: re.Match, body: dict, **_: Any) -> tuple[int, Any]:
    bond_id = match.group("bond_id")
    _bond_or_404(store, bond_id)
    events = body.get("events")
    if not isinstance(events, list):
        raise ApiError(400, "invalid_body", "events 必须是数组")
    try:
        result = ingest_events(store, bond_id, events, received_at=body.get("received_at"))
    except IngestError as exc:
        raise ApiError(400, "ingest_error", str(exc)) from exc
    return 200, result


def handle_evaluate(store: Store, match: re.Match, body: dict, **_: Any) -> tuple[int, Any]:
    bond_id = match.group("bond_id")
    _bond_or_404(store, bond_id)
    valuation_date = _require(body, "date")
    try:
        judgment = evaluate(store, bond_id, valuation_date, note=body.get("note", ""))
    except NotFoundError as exc:
        raise ApiError(404, "not_found", str(exc)) from exc
    except ClauseDependencyError as exc:
        raise ApiError(409, "clause_dependency_cycle", str(exc)) from exc
    except EvaluationError as exc:
        raise ApiError(400, "evaluation_error", str(exc)) from exc
    return 201, judgment


def handle_list_judgments(store: Store, match: re.Match, query: dict, **_: Any) -> tuple[int, Any]:
    bond_id = match.group("bond_id")
    _bond_or_404(store, bond_id)
    valuation_date = (query.get("valuation_date") or [None])[0]
    items = store.judgments_for(bond_id, valuation_date)
    summary = [
        {
            "judgment_id": j["judgment_id"],
            "valuation_date": j["valuation_date"],
            "version": j["version"],
            "data_version": j["data_version"],
            "supersedes": j.get("supersedes"),
            "data_stale": j["data_version"] < store.data_version,
        }
        for j in items
    ]
    return 200, {"bond_id": bond_id, "judgments": summary}


def handle_get_judgment(store: Store, match: re.Match, **_: Any) -> tuple[int, Any]:
    judgment = store.get_judgment(match.group("judgment_id"))
    if judgment is None:
        raise ApiError(404, "judgment_not_found", "判断版本不存在")
    return 200, judgment


def handle_gaps(store: Store, match: re.Match, query: dict, **_: Any) -> tuple[int, Any]:
    bond_id = match.group("bond_id")
    _bond_or_404(store, bond_id)
    date_str = (query.get("date") or [None])[0]
    if not date_str:
        raise ApiError(400, "missing_field", "缺少查询参数 date")
    try:
        return 200, scan_gaps(store, bond_id, date_str)
    except NotFoundError as exc:
        raise ApiError(404, "not_found", str(exc)) from exc
    except EvaluationError as exc:
        raise ApiError(400, "evaluation_error", str(exc)) from exc


def handle_create_reference(store: Store, body: dict, **_: Any) -> tuple[int, Any]:
    judgment_id = _require(body, "judgment_id")
    consumer = _require(body, "consumer")
    try:
        reference = add_reference(store, judgment_id, consumer, note=body.get("note", ""))
    except NotFoundError as exc:
        raise ApiError(404, "not_found", str(exc)) from exc
    return 201, reference


def handle_list_references(store: Store, query: dict, **_: Any) -> tuple[int, Any]:
    status = (query.get("status") or [None])[0]
    return 200, {"references": store.list_references(status=status)}


def handle_seed(store: Store, reference_dir: Path, **_: Any) -> tuple[int, Any]:
    return 200, seed_if_empty(store, reference_dir)


HandlerFn = Callable[..., tuple[int, Any]]
ROUTES: list[tuple[str, str, HandlerFn]] = [
    ("GET", r"/health", lambda **_: (200, health_payload())),
    ("POST", r"/admin/seed", handle_seed),
    ("GET", r"/bonds", handle_list_bonds),
    ("POST", r"/bonds", handle_create_bond),
    ("GET", r"/bonds/(?P<bond_id>[^/]+)", handle_get_bond),
    ("POST", r"/bonds/(?P<bond_id>[^/]+)/prices", handle_ingest_prices),
    ("POST", r"/bonds/(?P<bond_id>[^/]+)/events", handle_ingest_events),
    ("POST", r"/bonds/(?P<bond_id>[^/]+)/evaluate", handle_evaluate),
    ("GET", r"/bonds/(?P<bond_id>[^/]+)/judgments", handle_list_judgments),
    ("GET", r"/bonds/(?P<bond_id>[^/]+)/gaps", handle_gaps),
    ("GET", r"/judgments/(?P<judgment_id>[^/]+)", handle_get_judgment),
    ("POST", r"/references", handle_create_reference),
    ("GET", r"/references", handle_list_references),
]
COMPILED_ROUTES = [(method, re.compile("^" + pattern + "$"), fn) for method, pattern, fn in ROUTES]


class RequestHandler(BaseHTTPRequestHandler):
    store: Store
    reference_dir: Path
    lock: threading.Lock

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        body: dict[str, Any] = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._respond(400, {"error": {"code": "invalid_json", "message": "请求体不是合法 JSON"}})
                    return
        for route_method, pattern, handler in COMPILED_ROUTES:
            if route_method != method:
                continue
            matched = pattern.match(parsed.path)
            if not matched:
                continue
            try:
                kwargs: dict[str, Any] = {
                    "store": self.store,
                    "match": matched,
                    "query": query,
                    "body": body,
                    "reference_dir": self.reference_dir,
                }
                if method == "POST":
                    with self.lock:
                        status, payload = handler(**kwargs)
                else:
                    status, payload = handler(**kwargs)
            except ApiError as exc:
                self._respond(exc.status, {"error": {"code": exc.code, "message": exc.message}})
                return
            except Exception as exc:  # pragma: no cover - 兜底
                self._respond(500, {"error": {"code": "internal_error", "message": str(exc)}})
                return
            self._respond(status, payload)
            return
        self._respond(404, {"error": {"code": "not_found", "message": "接口不存在"}})

    def _respond(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, store: Store | None = None) -> ThreadingHTTPServer:
    if store is None:
        store = Store(os.getenv("CB_DATA_DIR", ".runtime"))
        seed_if_empty(store, DEFAULT_REFERENCE_DIR)
    handler = type(
        "BoundRequestHandler",
        (RequestHandler,),
        {"store": store, "reference_dir": DEFAULT_REFERENCE_DIR, "lock": threading.Lock()},
    )
    return ThreadingHTTPServer((host, port), handler)
