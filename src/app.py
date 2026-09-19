from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from cb.clauses import ClauseDependencyError
from cb.models import ValidationError
from cb.service import CbService
from cb.store import Store

SERVICE_NAME = '可转债条款分析服务'

ROOT = Path(__file__).resolve().parents[1]
STORE_DIR = Path(os.getenv("CB_STORE_DIR", ROOT / ".runtime"))
REFERENCE_DIR = Path(os.getenv("CB_REFERENCE_DIR", ROOT / "reference"))


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


def build_service() -> CbService:
    return CbService(Store(STORE_DIR), REFERENCE_DIR)


class Api:
    """路由表：每个 handler 接收 (service, match, query, body) 并返回 (status, payload)。"""

    def __init__(self, service: CbService):
        self.service = service
        self.routes: list[tuple[str, re.Pattern, object]] = [
            ("GET", re.compile(r"^/health$"), self.health),
            ("POST", re.compile(r"^/admin/seed$"), self.seed),
            ("GET", re.compile(r"^/bonds$"), self.list_bonds),
            ("POST", re.compile(r"^/bonds$"), self.create_bond),
            ("GET", re.compile(r"^/bonds/([^/]+)$"), self.get_bond),
            ("POST", re.compile(r"^/quotes$"), self.ingest_quotes),
            ("POST", re.compile(r"^/corporate-actions$"), self.ingest_corporate_action),
            ("POST", re.compile(r"^/suspensions$"), self.ingest_suspension),
            ("POST", re.compile(r"^/price-resets$"), self.ingest_price_reset),
            ("POST", re.compile(r"^/evaluate$"), self.evaluate),
            ("GET", re.compile(r"^/bonds/([^/]+)/judgments$"), self.list_judgments),
            ("GET", re.compile(r"^/judgments/([^/]+)$"), self.get_judgment),
            ("POST", re.compile(r"^/judgments/([^/]+)/replay$"), self.replay_judgment),
            ("POST", re.compile(r"^/references$"), self.create_reference),
            ("GET", re.compile(r"^/bonds/([^/]+)/references$"), self.list_references),
        ]

    # --- 基础 ---

    def health(self, m, q, body):
        return 200, health_payload()

    def seed(self, m, q, body):
        return 201, {"seeded": self.service.seed()}

    # --- 条款与数据摄入 ---

    def create_bond(self, m, q, body):
        record = self.service.ingest_terms(body)
        return 201, {"record_id": record["record_id"], "terms_version": record["record_version"]}

    def list_bonds(self, m, q, body):
        bonds = [
            {"bond_id": r["bond_id"], "name": r["name"], "underlying": r["underlying"],
             "terms_version": r["record_version"]}
            for r in self.service.store.state_as_of(kind="terms")
        ]
        return 200, {"bonds": sorted(bonds, key=lambda b: b["bond_id"])}

    def get_bond(self, m, q, body):
        bond_id = m.group(1)
        terms = next((r for r in self.service.store.state_as_of(kind="terms")
                      if r["bond_id"] == bond_id), None)
        if terms is None:
            return 404, {"error": f"未找到债券 {bond_id}"}
        return 200, terms

    def ingest_quotes(self, m, q, body):
        items = body if isinstance(body, list) else [body]
        result = self.service.ingest_quotes(items)
        return 201, {
            "ingested": [r["record_id"] for r in result["records"]],
            "reevaluated": result["reevaluated"],
        }

    def ingest_corporate_action(self, m, q, body):
        record = self.service.ingest_corporate_action(body)
        return 201, {"record_id": record["record_id"]}

    def ingest_suspension(self, m, q, body):
        record = self.service.ingest_suspension(body)
        return 201, {"record_id": record["record_id"]}

    def ingest_price_reset(self, m, q, body):
        record = self.service.ingest_price_reset(body)
        return 201, {"record_id": record["record_id"]}

    # --- 评估与复核 ---

    def evaluate(self, m, q, body):
        require = ["bond_id", "valuation_date"]
        missing = [f for f in require if not body.get(f)]
        if missing:
            raise ValidationError(f"缺少字段 {', '.join(missing)}")
        result = self.service.evaluate(body["bond_id"], body["valuation_date"])
        return (201 if result["created"] else 200), {
            "created": result["created"],
            "judgment": result["judgment"],
        }

    def list_judgments(self, m, q, body):
        bond_id = m.group(1)
        valuation_date = q.get("valuation_date", [None])[0]
        versions = self.service.store.judgments(bond_id, valuation_date)
        summaries = [
            {"judgment_id": j["judgment_id"], "version_no": j["version_no"],
             "valuation_date": j["valuation_date"], "created_at": j["created_at"],
             "content_hash": j["content_hash"], "supersedes": j["supersedes"]}
            for j in sorted(versions, key=lambda x: x["version_no"])
        ]
        return 200, {"judgments": summaries}

    def get_judgment(self, m, q, body):
        judgment = self.service.store.get_judgment(m.group(1))
        if judgment is None:
            return 404, {"error": "判断版本不存在"}
        return 200, judgment

    def replay_judgment(self, m, q, body):
        return 200, self.service.replay(m.group(1))

    # --- 结论引用 ---

    def create_reference(self, m, q, body):
        if not body.get("judgment_id") or not body.get("cited_by"):
            raise ValidationError("缺少字段 judgment_id, cited_by")
        ref = self.service.cite(body["judgment_id"], body["cited_by"], body.get("note"))
        return 201, ref

    def list_references(self, m, q, body):
        return 200, {"references": self.service.references(m.group(1))}


def make_handler(api: Api):
    class RequestHandler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            for route_method, pattern, handler in api.routes:
                if route_method != method:
                    continue
                match = pattern.match(parsed.path)
                if not match:
                    continue
                try:
                    body = self._read_body() if method == "POST" else {}
                    status, payload = handler(match, query, body)
                except ValidationError as exc:
                    status, payload = 400, {"error": str(exc)}
                except ClauseDependencyError as exc:
                    status, payload = 422, {"error": str(exc)}
                self._send(status, payload)
                return
            self._send(404, {"error": "Not Found"})

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValidationError(f"请求体不是合法 JSON: {exc}") from exc
            if not isinstance(data, (dict, list)):
                raise ValidationError("请求体需要 JSON 对象或数组")
            return data

        def _send(self, status: int, payload: dict) -> None:
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

    return RequestHandler


def create_server(host: str, port: int, service: CbService | None = None) -> ThreadingHTTPServer:
    api = Api(service or build_service())
    return ThreadingHTTPServer((host, port), make_handler(api))
