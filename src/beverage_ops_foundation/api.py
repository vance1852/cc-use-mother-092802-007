"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .retrofit_service import RetrofitService
from .service import DomainService
from .storage import Database


def _retrofit(service: DomainService) -> RetrofitService:
    """在同一数据库和时钟上构造技改核验服务。"""

    return RetrofitService(service.database, service.clock)


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        status, payload = _retrofit_route(_retrofit(service), method, parsed, body, actor_id)
        if status is not None:
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _retrofit_route(retrofit: RetrofitService, method: str, parsed, body: dict[str, Any],
                    actor_id: str) -> tuple[int | None, dict[str, Any]]:
    """分派技改收益核验相关路由。"""

    path = parsed.path
    parts = [segment for segment in path.split("/") if segment]
    query = parse_qs(parsed.query)

    if method == "POST":
        posting = {
            "/retrofit/boundaries": retrofit.register_boundary,
            "/retrofit/meters": retrofit.register_meter,
            "/retrofit/calibrations": retrofit.register_calibration,
            "/retrofit/batches": retrofit.register_batch,
            "/retrofit/readings": retrofit.record_reading,
            "/retrofit/factors": retrofit.propose_adjustment_factor,
            "/retrofit/factors/review": retrofit.review_adjustment_factor,
            "/retrofit/verifications": retrofit.freeze_verification,
            "/retrofit/alternates": retrofit.propose_alternate,
            "/retrofit/alternates/review": retrofit.review_alternate,
            "/retrofit/verifications/confirm": retrofit.confirm_verification,
            "/retrofit/verifications/close": retrofit.close_verification,
            "/retrofit/calibration-failures": retrofit.report_calibration_failure,
            "/retrofit/corrections/clear": retrofit.clear_correction,
        }
        handler = posting.get(path)
        if handler is not None:
            receipt = handler(actor_id=actor_id, **body)
            payload = {"request_id": receipt.request_id, "resource_type": receipt.resource_type,
                       "resource_id": receipt.resource_id, "replayed": receipt.replayed}
            if receipt.detail:
                payload.update(receipt.detail)
            return 200 if receipt.replayed else 201, payload
    if method == "GET" and path == "/retrofit/verifications":
        boundary_id = query.get("boundary_id", [""])[0]
        if not boundary_id:
            raise ValidationError("boundary_id 不能为空")
        return 200, {"items": retrofit.list_verifications(boundary_id)}
    if method == "GET" and len(parts) == 3 and parts[0] == "retrofit" \
            and parts[1] == "verifications":
        return 200, retrofit.get_verification(parts[2])
    if method == "GET" and len(parts) == 4 and parts[0] == "retrofit" \
            and parts[1] == "verifications" and parts[3] == "explain":
        return 200, retrofit.explain(parts[2])
    return None, {}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
