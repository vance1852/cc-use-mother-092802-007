"""技改收益核验的领域服务。

职责编排：
- 登记设备边界、计量点、校准证书、批次、蒸汽读数与经批准的调整因子；
- 以可注入时钟冻结数据快照，区分缺失/异常/迟到读数；
- 工程人员可提出替代计算，最终收益必须由非提单人的独立复核者确认；
- 计量失效（校准过期或失效窗口）定位受影响结论：未结算收益暂停，
  已关闭期间只能登记更正记录披露影响；
- 结果解释节省量、单位成本变化和置信边界分别来自哪些有效测量。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from beverage_ops_foundation.audit import append_event, canonical_json, digest
from beverage_ops_foundation.service import DomainService

from .engine import (
    ClassifiedBatch,
    classify_snapshot_entries,
    compute_savings,
    result_from_dict,
    result_to_dict,
)
from .models import (
    READING_OK,
    REVIEW_APPROVE,
    REVIEW_REJECT,
    VERIFICATION_CLOSED,
    VERIFICATION_CONFIRMED,
    VERIFICATION_DRAFT,
    VERIFICATION_REJECTED,
    VERIFICATION_SUBMITTED,
    VERIFICATION_SUSPENDED,
    CorrectionRecord,
    ReviewDecision,
    Snapshot,
    Verification,
)
from .storage import VerificationStorage

ENGINEERING_ROLES = ("operator", "admin")
REVIEW_ROLES = ("reviewer", "admin")
FACTOR_ROLES = ("reviewer", "admin")
EPOCH = "1970-01-01T00:00:00Z"


class VerificationService(DomainService):
    """在基础服务上扩展蒸汽技改收益核验工作流。"""

    def __init__(self, database, clock=None) -> None:
        super().__init__(database, clock)
        VerificationStorage(database)

    # ------------------------------------------------------------------ 工具

    def _timestamp(self, value: str, field: str) -> str:
        text = self._text(value, field, 40)
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            from beverage_ops_foundation.errors import ValidationError

            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            from beverage_ops_foundation.errors import ValidationError

            raise ValidationError(f"{field} 必须带时区")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _date_or_timestamp(self, value: str, field: str) -> str:
        return self._timestamp(value, field)

    def _positive(self, value: float, field: str) -> float:
        value = float(value)
        if value <= 0:
            from beverage_ops_foundation.errors import ValidationError

            raise ValidationError(f"{field} 必须为正数")
        return value

    def _load_verification(self, connection, verification_id: str):
        row = connection.execute(
            "SELECT * FROM verifications WHERE verification_id=?", (verification_id,)
        ).fetchone()
        if row is None:
            from beverage_ops_foundation.errors import NotFoundError

            raise NotFoundError("核验单不存在")
        return row

    def _load_boundary(self, connection, boundary_id: str):
        row = connection.execute(
            "SELECT * FROM equipment_boundaries WHERE boundary_id=?", (boundary_id,)
        ).fetchone()
        if row is None:
            from beverage_ops_foundation.errors import NotFoundError

            raise NotFoundError("设备边界不存在")
        return row

    def _check_site_org(self, connection, actor, site_id: str) -> None:
        from beverage_ops_foundation.errors import NotFoundError, PermissionDenied

        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")

    def _check_boundary_org(self, connection, actor, boundary_row) -> None:
        self._check_site_org(connection, actor, boundary_row["site_id"])

    def _audit(self, connection, *, actor_id, action, resource_type, resource_id, detail) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    # ---------------------------------------------------------- 边界与计量点

    def register_boundary(self, *, request_id: str, actor_id: str, site_id: str,
                          boundary_id: str, name: str, description: str = ""):
        payload = {"actor_id": actor_id, "site_id": site_id, "boundary_id": boundary_id,
                   "name": name, "description": description}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *ENGINEERING_ROLES)
            self._check_site_org(connection, actor, site_id)
            boundary_id = self._identifier(boundary_id, "boundary_id")
            name = self._text(name, "name")
            description = str(description).strip()

            def create():
                from beverage_ops_foundation.errors import ConflictError

                try:
                    connection.execute(
                        "INSERT INTO equipment_boundaries(boundary_id,site_id,name,description,version,"
                        "created_by,created_at) VALUES(?,?,?,?,1,?,?)",
                        (boundary_id, site_id, name, description, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("设备边界编号已存在或场所无效") from exc
                self._audit(connection, actor_id=actor_id, action="boundary.registered",
                            resource_type="equipment_boundary", resource_id=boundary_id,
                            detail={"site_id": site_id, "name": name})
                return "equipment_boundary", boundary_id, {"boundary_id": boundary_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_boundary", payload=payload, create=create)

    def register_meter_point(self, *, request_id: str, actor_id: str, boundary_id: str,
                             meter_id: str, name: str, metric: str, unit: str,
                             calibration_due: str):
        payload = {"actor_id": actor_id, "boundary_id": boundary_id, "meter_id": meter_id,
                   "name": name, "metric": metric, "unit": unit,
                   "calibration_due": calibration_due}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *ENGINEERING_ROLES)
            boundary = self._load_boundary(connection, boundary_id)
            self._check_boundary_org(connection, actor, boundary)
            meter_id = self._identifier(meter_id, "meter_id")
            name = self._text(name, "name")
            unit = self._text(unit, "unit", 40)
            if metric not in ("steam", "product"):
                from beverage_ops_foundation.errors import ValidationError

                raise ValidationError("metric 只能是 steam 或 product")
            calibration_due = self._date_or_timestamp(calibration_due, "calibration_due")

            def create():
                from beverage_ops_foundation.errors import ConflictError

                try:
                    connection.execute(
                        "INSERT INTO meter_points(meter_id,boundary_id,name,metric,unit,"
                        "calibration_due,active,version) VALUES(?,?,?,?,?,?,1,1)",
                        (meter_id, boundary_id, name, metric, unit, calibration_due),
                    )
                except Exception as exc:
                    raise ConflictError("计量点编号已存在或设备边界无效") from exc
                # 登记时声明的校准有效期，证书来源登记为初始声明，之后由校准记录续期。
                connection.execute(
                    "INSERT INTO calibrations(calibration_id,meter_id,certified_at,valid_until,"
                    "certificate_ref,created_at) VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, meter_id, EPOCH, calibration_due,
                     "登记时校准声明", self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="meter.registered",
                            resource_type="meter_point", resource_id=meter_id,
                            detail={"boundary_id": boundary_id, "metric": metric,
                                    "calibration_due": calibration_due})
                return "meter_point", meter_id, {"meter_id": meter_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_meter_point", payload=payload, create=create)

    def record_calibration(self, *, request_id: str, actor_id: str, meter_id: str,
                           certified_at: str, valid_until: str, certificate_ref: str,
                           calibration_id: str | None = None):
        payload = {"actor_id": actor_id, "meter_id": meter_id, "certified_at": certified_at,
                   "valid_until": valid_until, "certificate_ref": certificate_ref}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *ENGINEERING_ROLES)
            meter = connection.execute("SELECT * FROM meter_points WHERE meter_id=?",
                                       (meter_id,)).fetchone()
            if meter is None:
                from beverage_ops_foundation.errors import NotFoundError

                raise NotFoundError("计量点不存在")
            boundary = self._load_boundary(connection, meter["boundary_id"])
            self._check_boundary_org(connection, actor, boundary)
            certified_at = self._timestamp(certified_at, "certified_at")
            valid_until = self._timestamp(valid_until, "valid_until")
            if valid_until <= certified_at:
                from beverage_ops_foundation.errors import ValidationError

                raise ValidationError("valid_until 必须晚于 certified_at")
            certificate_ref = self._text(certificate_ref, "certificate_ref", 120)
            calibration_id = calibration_id or uuid.uuid4().hex

            def create():
                from beverage_ops_foundation.errors import ConflictError

                try:
                    connection.execute(
                        "INSERT INTO calibrations(calibration_id,meter_id,certified_at,valid_until,"
                        "certificate_ref,created_at) VALUES(?,?,?,?,?,?)",
                        (calibration_id, meter_id, certified_at, valid_until,
                         certificate_ref, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("校准记录编号已存在") from exc
                # 最新证书的有效期即计量点有效期（可能缩短，也可能延长）。
                connection.execute(
                    "UPDATE meter_points SET calibration_due=?, version=version+1 WHERE meter_id=?",
                    (valid_until, meter_id),
                )
                suspended, closed_hit = self._apply_meter_impact(connection, meter_id)
                self._audit(connection, actor_id=actor_id, action="calibration.recorded",
                            resource_type="calibration", resource_id=calibration_id,
                            detail={"meter_id": meter_id, "valid_until": valid_until,
                                    "certificate_ref": certificate_ref,
                                    "suspended_verifications": suspended,
                                    "verifications_requiring_correction": closed_hit})
                return "calibration", calibration_id, {
                    "calibration_id": calibration_id,
                    "suspended_verifications": suspended,
                    "verifications_requiring_correction": closed_hit}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_calibration", payload=payload, create=create)

    # ------------------------------------------------------------- 批次与读数

    def register_batch(self, *, request_id: str, actor_id: str, boundary_id: str,
                       batch_id: str, product_code: str, started_at: str, ended_at: str,
                       output: float):
        payload = {"actor_id": actor_id, "boundary_id": boundary_id, "batch_id": batch_id,
                   "product_code": product_code, "started_at": started_at,
                   "ended_at": ended_at, "output": output}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *ENGINEERING_ROLES)
            boundary = self._load_boundary(connection, boundary_id)
            self._check_boundary_org(connection, actor, boundary)
            batch_id = self._identifier(batch_id, "batch_id")
            product_code = self._text(product_code, "product_code", 60)
            started_at = self._timestamp(started_at, "started_at")
            ended_at = self._timestamp(ended_at, "ended_at")
            if ended_at <= started_at:
                from beverage_ops_foundation.errors import ValidationError

                raise ValidationError("ended_at 必须晚于 started_at")
            output = self._positive(output, "output")

            def create():
                from beverage_ops_foundation.errors import ConflictError

                try:
                    connection.execute(
                        "INSERT INTO production_batches(batch_id,boundary_id,product_code,"
                        "started_at,ended_at,output,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (batch_id, boundary_id, product_code, started_at, ended_at,
                         output, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批次编号已存在或设备边界无效") from exc
                self._audit(connection, actor_id=actor_id, action="batch.registered",
                            resource_type="production_batch", resource_id=batch_id,
                            detail={"boundary_id": boundary_id, "product_code": product_code,
                                    "output": output})
                return "production_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_batch", payload=payload, create=create)

    def record_reading(self, *, request_id: str, actor_id: str, batch_id: str,
                       meter_id: str, steam_kg: float, measured_at: str,
                       source_ref: str = "", reading_id: str | None = None):
        """追加一条蒸汽读数（同批次保留全部上报历史，按接收时间判定迟到）。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id, "meter_id": meter_id,
                   "steam_kg": steam_kg, "measured_at": measured_at, "source_ref": source_ref}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *ENGINEERING_ROLES)
            batch = connection.execute("SELECT * FROM production_batches WHERE batch_id=?",
                                       (batch_id,)).fetchone()
            if batch is None:
                from beverage_ops_foundation.errors import NotFoundError

                raise NotFoundError("批次不存在")
            meter = connection.execute("SELECT * FROM meter_points WHERE meter_id=?",
                                       (meter_id,)).fetchone()
            if meter is None:
                from beverage_ops_foundation.errors import NotFoundError

                raise NotFoundError("计量点不存在")
            boundary = self._load_boundary(connection, batch["boundary_id"])
            self._check_boundary_org(connection, actor, boundary)
            if meter["boundary_id"] != batch["boundary_id"] or meter["metric"] != "steam":
                from beverage_ops_foundation.errors import ValidationError

                raise ValidationError("计量点不属于该批次所在边界或不是蒸汽计量点")
            steam_kg = self._positive(steam_kg, "steam_kg")
            measured_at = self._timestamp(measured_at, "measured_at")
            reading_id = reading_id or uuid.uuid4().hex
            received_at = self._now()

            def create():
                from beverage_ops_foundation.errors import ConflictError

                try:
                    connection.execute(
                        "INSERT INTO steam_readings(reading_id,batch_id,meter_id,steam_kg,"
                        "measured_at,received_at,source_ref) VALUES(?,?,?,?,?,?,?)",
                        (reading_id, batch_id, meter_id, steam_kg, measured_at,
                         received_at, str(source_ref).strip()))
                except Exception as exc:
                    raise ConflictError("读数编号已存在") from exc
                self._audit(connection, actor_id=actor_id, action="reading.recorded",
                            resource_type="steam_reading", resource_id=reading_id,
                            detail={"batch_id": batch_id, "meter_id": meter_id,
                                    "steam_kg": steam_kg, "measured_at": measured_at,
                                    "received_at": received_at})
                return "steam_reading", reading_id, {"reading_id": reading_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_reading", payload=payload, create=create)

    # ----------------------------------------------------------- 调整因子

    def register_adjustment_factor(self, *, request_id: str, actor_id: str, boundary_id: str,
                                   code: str, value: float, reason: str,
                                   factor_id: str | None = None):
        """登记经批准的调整因子；批准权限属于复核角色，工程不能自批。"""

        payload = {"actor_id": actor_id, "boundary_id": boundary_id, "code": code,
                   "value": value, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *FACTOR_ROLES)
            boundary = self._load_boundary(connection, boundary_id)
            self._check_boundary_org(connection, actor, boundary)
            code = self._identifier(code, "code")
            value = self._positive(value, "value")
            reason = self._text(reason, "reason", 400)
            factor_id = factor_id or uuid.uuid4().hex

            def create():
                from beverage_ops_foundation.errors import ConflictError

                existing = connection.execute(
                    "SELECT factor_id,value FROM adjustment_factors WHERE boundary_id=? AND code=?",
                    (boundary_id, code),
                ).fetchone()
                if existing:
                    connection.execute(
                        "UPDATE adjustment_factors SET value=?,reason=?,approved_by=?,"
                        "approved_at=?,active=1 WHERE factor_id=?",
                        (value, reason, actor_id, self._now(), existing["factor_id"]),
                    )
                    factor_id_used = existing["factor_id"]
                else:
                    try:
                        connection.execute(
                            "INSERT INTO adjustment_factors(factor_id,boundary_id,code,value,"
                            "reason,approved_by,approved_at,active) VALUES(?,?,?,?,?,?,?,1)",
                            (factor_id, boundary_id, code, value, reason,
                             actor_id, self._now()),
                        )
                    except Exception as exc:
                        raise ConflictError("调整因子编号已存在") from exc
                    factor_id_used = factor_id
                self._audit(connection, actor_id=actor_id, action="adjustment_factor.approved",
                            resource_type="adjustment_factor", resource_id=factor_id_used,
                            detail={"boundary_id": boundary_id, "code": code, "value": value})
                return "adjustment_factor", factor_id_used, {"factor_id": factor_id_used}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_adjustment_factor", payload=payload,
                                    create=create)

    # ----------------------------------------------------------- 计量失效登记

    def report_meter_issue(self, *, request_id: str, actor_id: str, meter_id: str,
                           issue_from: str, issue_to: str, note: str,
                           issue_id: str | None = None) -> dict[str, Any]:
        """登记计量失效窗口，并立即把未结算的受影响收益暂停。"""

        payload = {"actor_id": actor_id, "meter_id": meter_id, "issue_from": issue_from,
                   "issue_to": issue_to, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *("operator", "reviewer", "admin"))
            meter = connection.execute("SELECT * FROM meter_points WHERE meter_id=?",
                                       (meter_id,)).fetchone()
            if meter is None:
                from beverage_ops_foundation.errors import NotFoundError

                raise NotFoundError("计量点不存在")
            boundary = self._load_boundary(connection, meter["boundary_id"])
            self._check_boundary_org(connection, actor, boundary)
            issue_from = self._timestamp(issue_from, "issue_from")
            issue_to = self._timestamp(issue_to, "issue_to")
            if issue_to <= issue_from:
                from beverage_ops_foundation.errors import ValidationError

                raise ValidationError("issue_to 必须晚于 issue_from")
            note = self._text(note, "note", 400)
            issue_id = issue_id or uuid.uuid4().hex

            def create():
                from beverage_ops_foundation.errors import ConflictError

                try:
                    connection.execute(
                        "INSERT INTO meter_issues(issue_id,meter_id,issue_from,issue_to,note,"
                        "reported_by,reported_at) VALUES(?,?,?,?,?,?,?)",
                        (issue_id, meter_id, issue_from, issue_to, note,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("失效记录编号已存在") from exc
                self._audit(connection, actor_id=actor_id, action="meter_issue.reported",
                            resource_type="meter_issue", resource_id=issue_id,
                            detail={"meter_id": meter_id, "issue_from": issue_from,
                                    "issue_to": issue_to})
                return "meter_issue", issue_id, {"issue_id": issue_id}

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="report_meter_issue", payload=payload, create=create)
            suspended, closed_hit = self._apply_meter_impact(
                connection, meter_id, issue_from=issue_from, issue_to=issue_to)
            return {
                "receipt": receipt.__dict__,
                "suspended_verifications": suspended,
                "verifications_requiring_correction": closed_hit,
            }

    def resolve_meter_issue(self, *, request_id: str, actor_id: str, issue_id: str):
        payload = {"actor_id": actor_id, "issue_id": issue_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *REVIEW_ROLES)
            issue = connection.execute("SELECT * FROM meter_issues WHERE issue_id=?",
                                       (issue_id,)).fetchone()
            if issue is None:
                from beverage_ops_foundation.errors import NotFoundError

                raise NotFoundError("失效记录不存在")

            def create():
                connection.execute(
                    "UPDATE meter_issues SET resolved_at=? WHERE issue_id=? AND resolved_at IS NULL",
                    (self._now(), issue_id),
                )
                self._audit(connection, actor_id=actor_id, action="meter_issue.resolved",
                            resource_type="meter_issue", resource_id=issue_id,
                            detail={"meter_id": issue["meter_id"]})
                return "meter_issue", issue_id, {"issue_id": issue_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_meter_issue", payload=payload, create=create)

    def check_calibration_validity(self, actor_id: str) -> dict[str, Any]:
        """按当前时钟找出校准已过期的计量点，暂停其未结算受影响收益。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "admin", "auditor", "operator")
            today = self._now()
            expired = [row["meter_id"] for row in connection.execute(
                "SELECT meter_id FROM meter_points WHERE active=1 AND calibration_due < ?",
                (today,))]
            suspended: list[str] = []
            closed_hit: list[str] = []
            for meter_id in expired:
                # 校准过期没有明确窗口：以“该表纳入结论的测量时刻晚于有效期”判定。
                s, c = self._apply_meter_impact(connection, meter_id)
                suspended.extend(s)
                closed_hit.extend(c)
            return {"as_of": today, "expired_meters": expired,
                    "suspended_verifications": sorted(set(suspended)),
                    "verifications_requiring_correction": sorted(set(closed_hit))}

    def _measurement_hits(self, connection, verification_row, meter_id: str,
                          issue_from: str | None, issue_to: str | None) -> bool:
        """该计量问题是否覆盖了核验采用的有效读数（快照内冻结的测量时刻）。"""

        snapshot_row = connection.execute(
            "SELECT payload_json FROM snapshots WHERE snapshot_id=?",
            (verification_row["snapshot_id"],)).fetchone()
        if snapshot_row is None:
            return False
        payload = json.loads(snapshot_row["payload_json"])
        entries = self._entries_from_payload(payload)
        # 校准有效性是会演变的事实（新证书可能缩短覆盖），定位影响时读取当前证书；
        # 同一签发时刻后登记的更正证书优先（按 rowid 排序取最新）。
        live_calibrations = [
            {"certified_at": c["certified_at"], "valid_until": c["valid_until"],
             "seq": c["rowid"]}
            for c in connection.execute(
                "SELECT rowid,certified_at,valid_until FROM calibrations "
                "WHERE meter_id=? ORDER BY rowid", (meter_id,))]
        # 只有真实有效（READING_OK）读数采用了该表测量值；被替代值顶替的
        # 异常/缺失批次不受该表失效影响（结论本来就没用它的读数）。
        for entry in entries:
            if entry.meter_id != meter_id or entry.reading_state != READING_OK:
                continue
            measured_at = entry.measured_at
            if measured_at is None:
                continue
            if issue_from is None:
                # 校准过期：测量时刻适用的最新证书（签发时刻相同则后登记的优先）
                # 的有效期截止日早于测量时刻。
                prior = [c for c in live_calibrations if c["certified_at"] <= measured_at]
                if prior and measured_at > max(
                        prior, key=lambda c: (c["certified_at"], c["seq"]))["valid_until"]:
                    return True
            elif issue_from <= measured_at <= issue_to:
                return True
        return False

    def _apply_meter_impact(self, connection, meter_id: str, *,
                            issue_from: str | None = None,
                            issue_to: str | None = None) -> tuple[list[str], list[str]]:
        """把计量失效传播到已确认结论：未结算暂停，已结算/已关闭列入更正披露。"""

        suspended: list[str] = []
        closed_hit: list[str] = []
        rows = connection.execute(
            "SELECT * FROM verifications WHERE status IN (?,?)",
            (VERIFICATION_CONFIRMED, VERIFICATION_CLOSED),
        ).fetchall()
        for row in rows:
            if not self._measurement_hits(connection, row, meter_id, issue_from, issue_to):
                continue
            if row["status"] == VERIFICATION_CLOSED or row["settle_state"] == "settled":
                closed_hit.append(row["verification_id"])
                continue
            cursor = connection.execute(
                "UPDATE verifications SET status=? WHERE verification_id=? AND status=?",
                (VERIFICATION_SUSPENDED, row["verification_id"], VERIFICATION_CONFIRMED),
            )
            if cursor.rowcount:
                self._audit(connection, actor_id="system", action="verification.suspended",
                            resource_type="verification", resource_id=row["verification_id"],
                            detail={"meter_id": meter_id, "reason": "meter_validity_failure"})
                suspended.append(row["verification_id"])
        return suspended, closed_hit

    def resume_verification(self, *, request_id: str, actor_id: str, verification_id: str,
                            comment: str = ""):
        """计量问题排除后，由复核角色把暂停的核验恢复为已确认（仍未结算）。"""

        payload = {"actor_id": actor_id, "verification_id": verification_id, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *REVIEW_ROLES)
            row = self._load_verification(connection, verification_id)
            if row["status"] != VERIFICATION_SUSPENDED:
                from beverage_ops_foundation.errors import ConflictError

                raise ConflictError("只有暂停状态的核验可以恢复")

            def create():
                connection.execute(
                    "UPDATE verifications SET status=? WHERE verification_id=?",
                    (VERIFICATION_CONFIRMED, verification_id),
                )
                self._audit(connection, actor_id=actor_id, action="verification.resumed",
                            resource_type="verification", resource_id=verification_id,
                            detail={"comment": comment})
                return "verification", verification_id, {"verification_id": verification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="resume_verification", payload=payload, create=create)

    # --------------------------------------------------------------- 核验单

    def create_verification(self, *, request_id: str, actor_id: str, boundary_id: str,
                            verification_id: str, title: str, baseline_start: str,
                            baseline_end: str, verification_start: str, verification_end: str,
                            confidence_level: float = 0.95):
        payload = {"actor_id": actor_id, "boundary_id": boundary_id,
                   "verification_id": verification_id, "title": title,
                   "baseline_start": baseline_start, "baseline_end": baseline_end,
                   "verification_start": verification_start, "verification_end": verification_end,
                   "confidence_level": confidence_level}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *ENGINEERING_ROLES)
            boundary = self._load_boundary(connection, boundary_id)
            self._check_boundary_org(connection, actor, boundary)
            verification_id = self._identifier(verification_id, "verification_id")
            title = self._text(title, "title")
            baseline_start = self._timestamp(baseline_start, "baseline_start")
            baseline_end = self._timestamp(baseline_end, "baseline_end")
            verification_start = self._timestamp(verification_start, "verification_start")
            verification_end = self._timestamp(verification_end, "verification_end")
            if not baseline_start < baseline_end < verification_start < verification_end:
                from beverage_ops_foundation.errors import ValidationError

                raise ValidationError("时间窗必须满足 基准起<基准止<验证起<验证止")
            confidence_level = float(confidence_level)
            if not 0.5 <= confidence_level < 1:
                from beverage_ops_foundation.errors import ValidationError

                raise ValidationError("confidence_level 必须位于 [0.5,1)")

            def create():
                from beverage_ops_foundation.errors import ConflictError

                try:
                    connection.execute(
                        "INSERT INTO verifications(verification_id,boundary_id,title,"
                        "baseline_start,baseline_end,verification_start,verification_end,"
                        "confidence_level,status,method,proposed_by,proposed_at,version) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
                        (verification_id, boundary_id, title, baseline_start, baseline_end,
                         verification_start, verification_end, confidence_level,
                         VERIFICATION_DRAFT, "standard", actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("核验单编号已存在") from exc
                self._audit(connection, actor_id=actor_id, action="verification.created",
                            resource_type="verification", resource_id=verification_id,
                            detail={"boundary_id": boundary_id, "title": title})
                return "verification", verification_id, {"verification_id": verification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_verification", payload=payload, create=create)

    def propose_alternative(self, *, request_id: str, actor_id: str, verification_id: str,
                            rationale: str, overrides: dict[str, float]):
        """工程人员提出替代计算：为异常/缺失批次给出替代蒸汽量并说明理由。"""

        if not isinstance(overrides, dict) or not overrides:
            from beverage_ops_foundation.errors import ValidationError

            raise ValidationError("overrides 必须是非空的 batch_id->蒸汽量 映射")
        payload = {"actor_id": actor_id, "verification_id": verification_id,
                   "rationale": rationale, "overrides": overrides}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *ENGINEERING_ROLES)
            row = self._load_verification(connection, verification_id)
            if row["status"] not in (VERIFICATION_DRAFT, VERIFICATION_REJECTED):
                from beverage_ops_foundation.errors import ConflictError

                raise ConflictError("只有草稿或驳回状态的核验可以提出替代计算")
            rationale = self._text(rationale, "rationale", 1000)
            normalized = {self._identifier(b, "batch_id"): self._positive(v, f"overrides.{b}")
                          for b, v in overrides.items()}
            boundary_batches = {
                r["batch_id"] for r in connection.execute(
                    "SELECT batch_id FROM production_batches WHERE boundary_id=?",
                    (row["boundary_id"],))
            }
            unknown = sorted(set(normalized) - boundary_batches)
            if unknown:
                from beverage_ops_foundation.errors import ValidationError

                raise ValidationError(f"替代批次不属于该设备边界: {','.join(unknown)}")

            def create():
                connection.execute(
                    "UPDATE verifications SET method='engineering_alternative',"
                    "engineering_rationale=?,version=version+1 WHERE verification_id=?",
                    (rationale, verification_id),
                )
                for batch_id, steam_kg in normalized.items():
                    connection.execute(
                        "INSERT INTO engineering_overrides(override_id,verification_id,batch_id,"
                        "steam_kg,rationale,created_by,created_at) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(verification_id,batch_id) DO UPDATE SET "
                        "steam_kg=excluded.steam_kg,rationale=excluded.rationale,"
                        "created_by=excluded.created_by,created_at=excluded.created_at",
                        (uuid.uuid4().hex, verification_id, batch_id, steam_kg,
                         rationale, actor_id, self._now()),
                    )
                self._audit(connection, actor_id=actor_id, action="verification.alternative_proposed",
                            resource_type="verification", resource_id=verification_id,
                            detail={"batches": sorted(normalized), "rationale": rationale})
                return "verification", verification_id, {"verification_id": verification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="propose_alternative", payload=payload, create=create)

    def withdraw_alternative(self, *, request_id: str, actor_id: str, verification_id: str):
        payload = {"actor_id": actor_id, "verification_id": verification_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *ENGINEERING_ROLES)
            row = self._load_verification(connection, verification_id)
            if row["status"] not in (VERIFICATION_DRAFT, VERIFICATION_REJECTED):
                from beverage_ops_foundation.errors import ConflictError

                raise ConflictError("当前状态不能撤回替代计算")

            def create():
                connection.execute("DELETE FROM engineering_overrides WHERE verification_id=?",
                                   (verification_id,))
                connection.execute(
                    "UPDATE verifications SET method='standard',engineering_rationale='',"
                    "version=version+1 WHERE verification_id=?",
                    (verification_id,),
                )
                self._audit(connection, actor_id=actor_id, action="verification.alternative_withdrawn",
                            resource_type="verification", resource_id=verification_id, detail={})
                return "verification", verification_id, {"verification_id": verification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_alternative", payload=payload, create=create)

    def _build_snapshot_payload(self, connection, row) -> dict[str, Any]:
        frozen_at = self._now()
        boundary_id = row["boundary_id"]
        batches_raw = connection.execute(
            "SELECT * FROM production_batches WHERE boundary_id=? AND "
            "(started_at BETWEEN ? AND ? OR started_at BETWEEN ? AND ?) ORDER BY started_at",
            (boundary_id, row["baseline_start"], row["baseline_end"],
             row["verification_start"], row["verification_end"]),
        ).fetchall()
        batch_ids = [b["batch_id"] for b in batches_raw]
        batches: list[dict[str, Any]] = []
        for b in batches_raw:
            if b["started_at"] <= row["baseline_end"]:
                period = "baseline"
            else:
                period = "verification"
            batches.append({"batch_id": b["batch_id"], "product_code": b["product_code"],
                            "started_at": b["started_at"], "ended_at": b["ended_at"],
                            "output": b["output"], "period": period})
        if batch_ids:
            placeholders = ",".join("?" for _ in batch_ids)
            readings_raw = connection.execute(
                f"SELECT * FROM steam_readings WHERE batch_id IN ({placeholders}) "
                f"AND received_at <= ? ORDER BY received_at",
                (*batch_ids, frozen_at),
            ).fetchall()
        else:
            readings_raw = []
        readings = [{"reading_id": r["reading_id"], "batch_id": r["batch_id"],
                     "meter_id": r["meter_id"], "steam_kg": r["steam_kg"],
                     "measured_at": r["measured_at"], "received_at": r["received_at"]}
                    for r in readings_raw]
        meters_raw = connection.execute(
            "SELECT * FROM meter_points WHERE boundary_id=? AND active=1", (boundary_id,)).fetchall()
        meters = [{"meter_id": m["meter_id"], "name": m["name"], "metric": m["metric"],
                   "unit": m["unit"], "calibration_due": m["calibration_due"]} for m in meters_raw]
        calibrations: dict[str, list[dict[str, Any]]] = {}
        for m in meters_raw:
            calibrations[m["meter_id"]] = [
                {"certified_at": c["certified_at"], "valid_until": c["valid_until"],
                 "certificate_ref": c["certificate_ref"], "seq": c["rowid"]}
                for c in connection.execute(
                    "SELECT rowid,certified_at,valid_until,certificate_ref FROM calibrations "
                    "WHERE meter_id=? ORDER BY rowid", (m["meter_id"],))]
        issues_raw = connection.execute(
            "SELECT * FROM meter_issues WHERE meter_id IN "
            "(SELECT meter_id FROM meter_points WHERE boundary_id=?) AND reported_at <= ?",
            (boundary_id, frozen_at),
        ).fetchall()
        issues = [{"issue_id": i["issue_id"], "meter_id": i["meter_id"],
                   "issue_from": i["issue_from"], "issue_to": i["issue_to"]} for i in issues_raw]
        factors = {f["code"]: f["value"] for f in connection.execute(
            "SELECT code,value FROM adjustment_factors WHERE boundary_id=? AND active=1",
            (boundary_id,))}
        overrides = {o["batch_id"]: o["steam_kg"] for o in connection.execute(
            "SELECT batch_id,steam_kg FROM engineering_overrides WHERE verification_id=?",
            (row["verification_id"],))}
        return {
            "boundary_id": boundary_id,
            "frozen_at": frozen_at,
            "window": {"baseline_start": row["baseline_start"], "baseline_end": row["baseline_end"],
                       "verification_start": row["verification_start"],
                       "verification_end": row["verification_end"]},
            "confidence_level": row["confidence_level"],
            "meters": meters,
            "calibrations": calibrations,
            "batches": batches,
            "readings": readings,
            "issues": issues,
            "factors": factors,
            "overrides": overrides,
        }

    def _entries_from_payload(self, payload: dict[str, Any], *, include_late_readings=None,
                              cutoff_at: str | None = None) -> list[ClassifiedBatch]:
        cutoff = cutoff_at or payload["frozen_at"]
        readings = list(payload["readings"])
        if include_late_readings:
            readings = readings + list(include_late_readings)
        windows = [(i["meter_id"], i["issue_from"], i["issue_to"]) for i in payload["issues"]]
        return classify_snapshot_entries(
            batches=payload["batches"], readings=readings,
            calibrations_by_meter=payload["calibrations"],
            active_issue_meter_windows=windows, cutoff_at=cutoff,
            overrides=payload.get("overrides", {}))

    def freeze_snapshot(self, *, request_id: str, actor_id: str, verification_id: str):
        """冻结当前数据并计算标准口径与（若有）工程替代口径结果。"""

        payload_in = {"actor_id": actor_id, "verification_id": verification_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *ENGINEERING_ROLES)
            row = self._load_verification(connection, verification_id)
            if row["status"] not in (VERIFICATION_DRAFT, VERIFICATION_REJECTED):
                from beverage_ops_foundation.errors import ConflictError

                raise ConflictError("只有草稿或驳回状态可以冻结快照")
            boundary = self._load_boundary(connection, row["boundary_id"])
            self._check_boundary_org(connection, actor, boundary)

            def create():
                from beverage_ops_foundation.errors import ConflictError, ValidationError

                payload = self._build_snapshot_payload(connection, row)
                if not any(b["period"] == "baseline" for b in payload["batches"]) or \
                   not any(b["period"] == "verification" for b in payload["batches"]):
                    raise ValidationError("基准期与验证期都必须至少包含一个批次")
                entries = self._entries_from_payload(payload)
                # 标准口径：只用真实有效测量，忽略任何工程替代值。始终计算供复核对照。
                standard = compute_savings(entries, payload["factors"], method="standard",
                                           confidence_level=payload["confidence_level"])
                # 最终采用口径：若工程提出替代计算，替代值可顶替缺失/异常批次。
                effective = (compute_savings(
                    entries, payload["factors"], method="engineering_alternative",
                    confidence_level=payload["confidence_level"])
                    if row["method"] == "engineering_alternative" else standard)
                if effective.baseline_n < 2 or effective.verification_n < 2:
                    raise ValidationError(
                        f"采用口径有效测量不足：基准期 {effective.baseline_n} 个、验证期 "
                        f"{effective.verification_n} 个，均需至少 2 个有效批次")
                standard_warning = (
                    None if standard.baseline_n >= 2 and standard.verification_n >= 2
                    else "standard_insufficient_measurements")
                snapshot_id = uuid.uuid4().hex
                payload_hash = digest(payload)
                connection.execute(
                    "INSERT INTO snapshots(snapshot_id,verification_id,frozen_at,cutoff_at,"
                    "payload_json,payload_hash) VALUES(?,?,?,?,?,?)",
                    (snapshot_id, verification_id, payload["frozen_at"], payload["frozen_at"],
                     canonical_json(payload), payload_hash),
                )
                connection.execute(
                    "UPDATE verifications SET snapshot_id=?,standard_result_json=?,"
                    "result_json=?,status=?,version=version+1 WHERE verification_id=?",
                    (snapshot_id, canonical_json(result_to_dict(standard)),
                     canonical_json(result_to_dict(effective)), VERIFICATION_DRAFT,
                     verification_id),
                )
                self._audit(connection, actor_id=actor_id, action="snapshot.frozen",
                            resource_type="snapshot", resource_id=snapshot_id,
                            detail={"verification_id": verification_id,
                                    "payload_hash": payload_hash,
                                    "method": effective.method,
                                    "standard_warning": standard_warning,
                                    "missing": effective.missing_count,
                                    "anomalous": effective.anomalous_count,
                                    "late": effective.late_count})
                return "snapshot", snapshot_id, {"snapshot_id": snapshot_id,
                                                 "payload_hash": payload_hash,
                                                 "standard_warning": standard_warning}

            return self._idempotent(connection, request_id=request_id,
                                    action="freeze_snapshot", payload=payload_in, create=create)

    def submit_for_review(self, *, request_id: str, actor_id: str, verification_id: str):
        payload = {"actor_id": actor_id, "verification_id": verification_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *ENGINEERING_ROLES)
            row = self._load_verification(connection, verification_id)
            if row["status"] not in (VERIFICATION_DRAFT, VERIFICATION_REJECTED):
                from beverage_ops_foundation.errors import ConflictError

                raise ConflictError("只有草稿/驳回状态且已冻结快照的核验可以提交")
            if not row["snapshot_id"]:
                from beverage_ops_foundation.errors import ValidationError

                raise ValidationError("提交前必须先冻结数据快照")

            def create():
                connection.execute(
                    "UPDATE verifications SET status=?,submitted_at=?,version=version+1 "
                    "WHERE verification_id=?",
                    (VERIFICATION_SUBMITTED, self._now(), verification_id),
                )
                self._audit(connection, actor_id=actor_id, action="verification.submitted",
                            resource_type="verification", resource_id=verification_id,
                            detail={"snapshot_id": row["snapshot_id"]})
                return "verification", verification_id, {"verification_id": verification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_for_review", payload=payload, create=create)

    def review_verification(self, *, request_id: str, actor_id: str, verification_id: str,
                            decision: str, comment: str = ""):
        """独立复核：reviewer 确认或驳回；复核者不能是提单人。"""

        payload = {"actor_id": actor_id, "verification_id": verification_id,
                   "decision": decision, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *REVIEW_ROLES)
            row = self._load_verification(connection, verification_id)
            if row["status"] != VERIFICATION_SUBMITTED:
                from beverage_ops_foundation.errors import ConflictError

                raise ConflictError("只有待复核状态的核验可以给出结论")
            if actor_id == row["proposed_by"]:
                from beverage_ops_foundation.errors import PermissionDenied

                raise PermissionDenied("独立复核者不能是替代计算的提单人")
            if decision not in (REVIEW_APPROVE, REVIEW_REJECT):
                from beverage_ops_foundation.errors import ValidationError

                raise ValidationError("decision 只能是 approve 或 reject")
            comment = str(comment).strip()
            new_status = VERIFICATION_CONFIRMED if decision == REVIEW_APPROVE else VERIFICATION_REJECTED

            def create():
                # 回调内重新校验状态，保证重放时幂等回执先命中。
                fresh = self._load_verification(connection, verification_id)
                if fresh["status"] != VERIFICATION_SUBMITTED:
                    from beverage_ops_foundation.errors import ConflictError

                    raise ConflictError("只有待复核状态的核验可以给出结论")
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO reviews(review_id,verification_id,snapshot_id,decision,"
                    "reviewer_id,comment,decided_at) VALUES(?,?,?,?,?,?,?)",
                    (review_id, verification_id, fresh["snapshot_id"], decision,
                     actor_id, comment, self._now()),
                )
                connection.execute(
                    "UPDATE verifications SET status=?,confirmed_by=?,confirmed_at=?,"
                    "version=version+1 WHERE verification_id=?",
                    (new_status, actor_id if decision == REVIEW_APPROVE else None,
                     self._now() if decision == REVIEW_APPROVE else None, verification_id),
                )
                self._audit(connection, actor_id=actor_id,
                            action=f"verification.{decision}",
                            resource_type="verification", resource_id=verification_id,
                            detail={"review_id": review_id, "comment": comment,
                                    "snapshot_id": fresh["snapshot_id"]})
                return "review", review_id, {"review_id": review_id, "decision": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="review_verification", payload=payload, create=create)

    def settle_benefit(self, *, request_id: str, actor_id: str, verification_id: str):
        """把已确认收益标记为已结算；暂停状态不得结算。"""

        payload = {"actor_id": actor_id, "verification_id": verification_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            row = self._load_verification(connection, verification_id)
            if row["status"] != VERIFICATION_CONFIRMED:
                from beverage_ops_foundation.errors import ConflictError

                raise ConflictError("只有已确认且未暂停的收益可以结算")

            def create():
                connection.execute(
                    "UPDATE verifications SET settle_state='settled',settled_at=? "
                    "WHERE verification_id=?",
                    (self._now(), verification_id),
                )
                self._audit(connection, actor_id=actor_id, action="benefit.settled",
                            resource_type="verification", resource_id=verification_id, detail={})
                return "verification", verification_id, {"verification_id": verification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="settle_benefit", payload=payload, create=create)

    def close_period(self, *, request_id: str, actor_id: str, verification_id: str):
        """关闭会计期间；关闭后只能通过更正记录披露计量影响。"""

        payload = {"actor_id": actor_id, "verification_id": verification_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            row = self._load_verification(connection, verification_id)
            if row["status"] != VERIFICATION_CONFIRMED:
                from beverage_ops_foundation.errors import ConflictError

                raise ConflictError("只有已确认的核验可以关闭期间")

            def create():
                connection.execute(
                    "UPDATE verifications SET status=?,closed_at=?,version=version+1 "
                    "WHERE verification_id=?",
                    (VERIFICATION_CLOSED, self._now(), verification_id),
                )
                self._audit(connection, actor_id=actor_id, action="period.closed",
                            resource_type="verification", resource_id=verification_id, detail={})
                return "verification", verification_id, {"verification_id": verification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="close_period", payload=payload, create=create)

    def record_correction(self, *, request_id: str, actor_id: str, verification_id: str,
                          issue_id: str, reason: str, disclosed_impact: dict[str, Any]):
        """已关闭期间发现计量问题：只登记更正披露，不改动冻结结果。"""

        if not isinstance(disclosed_impact, dict) or not disclosed_impact:
            from beverage_ops_foundation.errors import ValidationError

            raise ValidationError("disclosed_impact 必须是非空对象")
        payload = {"actor_id": actor_id, "verification_id": verification_id,
                   "issue_id": issue_id, "reason": reason, "disclosed_impact": disclosed_impact}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *REVIEW_ROLES)
            row = self._load_verification(connection, verification_id)
            if row["status"] != VERIFICATION_CLOSED:
                from beverage_ops_foundation.errors import ConflictError

                raise ConflictError("更正记录只能登记在已经关闭的期间")
            issue = connection.execute("SELECT * FROM meter_issues WHERE issue_id=?",
                                       (issue_id,)).fetchone()
            if issue is None:
                from beverage_ops_foundation.errors import NotFoundError

                raise NotFoundError("计量失效记录不存在")
            reason = self._text(reason, "reason", 1000)

            def create():
                correction_id = uuid.uuid4().hex
                impacted = [issue["meter_id"]]
                connection.execute(
                    "INSERT INTO corrections(correction_id,verification_id,issue_id,reason,"
                    "impacted_meter_ids_json,disclosed_impact_json,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (correction_id, verification_id, issue_id, reason,
                     canonical_json(impacted), canonical_json(disclosed_impact),
                     actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="correction.recorded",
                            resource_type="correction", resource_id=correction_id,
                            detail={"verification_id": verification_id, "issue_id": issue_id,
                                    "impacted_meter_ids": impacted,
                                    "disclosed_impact_hash": digest(disclosed_impact)})
                return "correction", correction_id, {"correction_id": correction_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_correction", payload=payload, create=create)

    # ----------------------------------------------------------------- 查询

    def get_snapshot(self, verification_id: str) -> Snapshot:
        row = self.database.connection.execute(
            "SELECT * FROM snapshots WHERE verification_id=? ORDER BY frozen_at DESC LIMIT 1",
            (verification_id,)).fetchone()
        if row is None:
            from beverage_ops_foundation.errors import NotFoundError

            raise NotFoundError("核验单尚未冻结快照")
        payload = json.loads(row["payload_json"])
        if digest(payload) != row["payload_hash"]:
            from beverage_ops_foundation.errors import ConflictError

            raise ConflictError("快照载荷哈希校验失败，数据可能被篡改")
        window = payload["window"]
        return Snapshot(row["snapshot_id"], row["verification_id"], payload["boundary_id"],
                        row["frozen_at"], window["baseline_start"], window["baseline_end"],
                        window["verification_start"], window["verification_end"],
                        payload, row["payload_hash"])

    def get_verification(self, verification_id: str) -> Verification:
        row = self.database.connection.execute(
            "SELECT * FROM verifications WHERE verification_id=?", (verification_id,)).fetchone()
        if row is None:
            from beverage_ops_foundation.errors import NotFoundError

            raise NotFoundError("核验单不存在")
        result = result_from_dict(json.loads(row["result_json"])) if row["result_json"] else None
        return Verification(
            verification_id=row["verification_id"], boundary_id=row["boundary_id"],
            period=f"{row['baseline_start']}/{row['verification_end']}", title=row["title"],
            status=row["status"], method=row["method"],
            engineering_rationale=row["engineering_rationale"], proposed_by=row["proposed_by"],
            proposed_at=row["proposed_at"], submitted_at=row["submitted_at"],
            confirmed_by=row["confirmed_by"], confirmed_at=row["confirmed_at"],
            snapshot_id=row["snapshot_id"], result=result, settle_state=row["settle_state"],
            closed_at=row["closed_at"])

    def list_reviews(self, verification_id: str) -> list[ReviewDecision]:
        rows = self.database.connection.execute(
            "SELECT * FROM reviews WHERE verification_id=? ORDER BY decided_at",
            (verification_id,)).fetchall()
        return [ReviewDecision(r["review_id"], r["verification_id"], r["decision"],
                               r["reviewer_id"], r["comment"], r["decided_at"]) for r in rows]

    def list_corrections(self, verification_id: str) -> list[CorrectionRecord]:
        rows = self.database.connection.execute(
            "SELECT * FROM corrections WHERE verification_id=? ORDER BY recorded_at",
            (verification_id,)).fetchall()
        return [CorrectionRecord(r["correction_id"], r["verification_id"], r["reason"],
                                json.loads(r["impacted_meter_ids_json"]),
                                json.loads(r["disclosed_impact_json"]),
                                r["recorded_by"], r["recorded_at"]) for r in rows]

    def reading_quality(self, verification_id: str) -> dict[str, Any]:
        """返回快照口径的读数质量，并标注冻结之后到达的迟到读数。"""

        snapshot = self.get_snapshot(verification_id)
        payload = snapshot.payload
        batch_ids = [b["batch_id"] for b in payload["batches"]]
        late_readings: list[dict[str, Any]] = []
        if batch_ids:
            placeholders = ",".join("?" for _ in batch_ids)
            late_readings = [
                {"reading_id": r["reading_id"], "batch_id": r["batch_id"],
                 "meter_id": r["meter_id"], "steam_kg": r["steam_kg"],
                 "measured_at": r["measured_at"], "received_at": r["received_at"]}
                for r in self.database.connection.execute(
                    f"SELECT * FROM steam_readings WHERE batch_id IN ({placeholders}) "
                    f"AND received_at > ? ORDER BY received_at",
                    (*batch_ids, snapshot.frozen_at))]
        frozen_entries = self._entries_from_payload(payload)
        current_entries = self._entries_from_payload(
            payload, include_late_readings=late_readings)
        return {
            "snapshot_id": snapshot.snapshot_id,
            "frozen_at": snapshot.frozen_at,
            "late_arrivals": late_readings,
            "frozen_quality": [e.__dict__ for e in frozen_entries],
            "current_quality": [e.__dict__ for e in current_entries],
        }

    def replay_from_snapshot(self, verification_id: str) -> SavingsResult:
        """完全依据冻结快照复算结果，用于离线核验数字来源。"""

        snapshot = self.get_snapshot(verification_id)
        payload = snapshot.payload
        entries = self._entries_from_payload(payload)
        method = self.get_verification(verification_id).method
        return compute_savings(entries, payload["factors"], method=method,
                               confidence_level=payload["confidence_level"])

    def explain(self, verification_id: str) -> dict[str, Any]:
        """解释节省量、单位成本变化、置信边界来自哪些有效测量。"""

        verification = self.get_verification(verification_id)
        snapshot = self.get_snapshot(verification_id)
        payload = snapshot.payload
        entries = self._entries_from_payload(payload)
        row = self._load_verification(self.database.connection, verification_id)
        standard_result = (json.loads(row["standard_result_json"])
                           if row["standard_result_json"] else None)

        def included(entry) -> bool:
            if entry.reading_state == READING_OK:
                return True
            return (verification.method == "engineering_alternative"
                    and entry.overridden_steam_kg is not None
                    and entry.reading_state in ("missing", "anomalous"))

        effective_batches = sorted(e.batch_id for e in entries if included(e))
        calib_used = {}
        for reading in payload["readings"]:
            if reading["batch_id"] not in effective_batches:
                continue
            prior = [c for c in payload["calibrations"].get(reading["meter_id"], [])
                     if c["certified_at"] <= reading["measured_at"]]
            if prior and reading["measured_at"] <= max(
                    prior, key=lambda c: (c["certified_at"], c.get("seq", 0)))["valid_until"]:
                chosen = max(prior, key=lambda c: (c["certified_at"], c.get("seq", 0)))
                calib_used[reading["meter_id"]] = chosen["certificate_ref"]
        quality = self.reading_quality(verification_id)
        late_batches = sorted({r["batch_id"] for r in quality["late_arrivals"]})
        return {
            "verification_id": verification_id,
            "status": verification.status,
            "method": verification.method,
            "settle_state": verification.settle_state,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.payload_hash,
            "frozen_at": snapshot.frozen_at,
            "result": result_to_dict(verification.result) if verification.result else None,
            "standard_baseline_result": standard_result,
            "effective_measurement_batches": sorted(effective_batches),
            "calibration_certificates_used": calib_used,
            "approved_adjustment_factors": payload["factors"],
            "engineering_overrides": payload.get("overrides", {}),
            "excluded": {
                "missing_batches": [e.batch_id for e in entries
                                    if e.reading_state == "missing"],
                "anomalous_batches": [e.batch_id for e in entries
                                      if e.reading_state == "anomalous"],
                "late_batches": late_batches,
            },
            "late_arrivals_after_freeze": quality["late_arrivals"],
            "reviews": [r.__dict__ for r in self.list_reviews(verification_id)],
            "corrections": [c.__dict__ for c in self.list_corrections(verification_id)],
        }
