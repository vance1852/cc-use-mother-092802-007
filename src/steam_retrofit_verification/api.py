"""技改收益核验服务的 HTTP/JSON 边界（仅依赖标准库）。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from beverage_ops_foundation.api import route as foundation_route
from beverage_ops_foundation.errors import DomainError, ValidationError
from beverage_ops_foundation.storage import Database

from .models import Snapshot, Verification
from .service import VerificationService


def _snapshot_dict(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "verification_id": snapshot.verification_id,
        "boundary_id": snapshot.boundary_id,
        "frozen_at": snapshot.frozen_at,
        "baseline_start": snapshot.baseline_start,
        "baseline_end": snapshot.baseline_end,
        "verification_start": snapshot.verification_start,
        "verification_end": snapshot.verification_end,
        "payload_hash": snapshot.payload_hash,
        "batch_count": len(snapshot.payload["batches"]),
        "reading_count": len(snapshot.payload["readings"]),
    }


def route(service: VerificationService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把核验相关请求分派到 VerificationService，其余转发到基础服务路由。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")

    def arg(name: str, default: str = "") -> str:
        return query.get(name, [default])[0]

    try:
        p = parsed.path
        if method == "POST" and p == "/boundaries":
            receipt = service.register_boundary(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/meters":
            receipt = service.register_meter_point(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/calibrations":
            receipt = service.record_calibration(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/batches":
            receipt = service.register_batch(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/readings":
            receipt = service.record_reading(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/adjustment-factors":
            receipt = service.register_adjustment_factor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/meter-issues":
            result = service.report_meter_issue(actor_id=actor_id, **body)
            return 200 if result["receipt"]["replayed"] else 201, result
        if method == "POST" and p == "/meter-issues/resolve":
            receipt = service.resolve_meter_issue(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/calibration-check":
            return 200, service.check_calibration_validity(actor_id=actor_id)
        if method == "POST" and p == "/verifications":
            receipt = service.create_verification(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/verifications/alternative":
            receipt = service.propose_alternative(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/verifications/alternative/withdraw":
            receipt = service.withdraw_alternative(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 200, receipt.__dict__
        if method == "POST" and p == "/snapshots/freeze":
            receipt = service.freeze_snapshot(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/verifications/submit":
            receipt = service.submit_for_review(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 200, receipt.__dict__
        if method == "POST" and p == "/verifications/review":
            receipt = service.review_verification(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and p == "/verifications/settle":
            receipt = service.settle_benefit(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 200, receipt.__dict__
        if method == "POST" and p == "/verifications/close":
            receipt = service.close_period(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 200, receipt.__dict__
        if method == "POST" and p == "/verifications/resume":
            receipt = service.resume_verification(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 200, receipt.__dict__
        if method == "POST" and p == "/verifications/corrections":
            receipt = service.record_correction(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and p == "/verifications":
            verification_id = arg("verification_id")
            if not verification_id:
                raise ValidationError("verification_id 不能为空")
            return 200, _verification_dict(service.get_verification(verification_id))
        if method == "GET" and p == "/verifications/explain":
            verification_id = arg("verification_id")
            if not verification_id:
                raise ValidationError("verification_id 不能为空")
            return 200, service.explain(verification_id)
        if method == "GET" and p == "/verifications/reading-quality":
            verification_id = arg("verification_id")
            if not verification_id:
                raise ValidationError("verification_id 不能为空")
            return 200, service.reading_quality(verification_id)
        if method == "GET" and p == "/snapshots":
            verification_id = arg("verification_id")
            if not verification_id:
                raise ValidationError("verification_id 不能为空")
            return 200, _snapshot_dict(service.get_snapshot(verification_id))
        # 未命中核验路由时转发给基础服务（组织、操作者、场所、健康检查等）。
        return foundation_route(service, method, path, body, headers)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _verification_dict(v: Verification) -> dict[str, Any]:
    from .engine import result_to_dict

    return {
        "verification_id": v.verification_id,
        "boundary_id": v.boundary_id,
        "period": v.period,
        "title": v.title,
        "status": v.status,
        "method": v.method,
        "engineering_rationale": v.engineering_rationale,
        "proposed_by": v.proposed_by,
        "proposed_at": v.proposed_at,
        "submitted_at": v.submitted_at,
        "confirmed_by": v.confirmed_by,
        "confirmed_at": v.confirmed_at,
        "snapshot_id": v.snapshot_id,
        "settle_state": v.settle_state,
        "closed_at": v.closed_at,
        "result": result_to_dict(v.result) if v.result else None,
    }


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为核验路由调用。"""

    service: VerificationService

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
    parser = argparse.ArgumentParser(description="启动蒸汽技改收益核验服务")
    parser.add_argument("--database", default="verification.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = VerificationService(database)
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
