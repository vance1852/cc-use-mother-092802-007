"""提供酒厂蒸汽利用技改的节能收益核验能力。

在基础服务的操作者、场所、权限、幂等和审计链之上增加：
设备边界与计量点登记、校准有效期、基准期/验证期批次、
批准的调整因子、读数质量判定（缺失/异常/迟到）、
冻结数据快照、替代计算与独立复核确认、校准失效影响定位，
以及节省量、单位成本变化和置信边界的来源解释。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .statistics import ValidMeasurement, calculate_savings
from .storage import Database

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

PERIOD_KINDS = ("baseline", "verification")
METER_TYPES = ("steam", "output", "energy")


def _parse_dt(value: str, field: str) -> datetime:
    """解析 ISO 8601 时间为带时区的 datetime。"""

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _dt_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RetrofitService:
    """协调技改核验的登记、计算、快照、复核与更正规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 通用辅助
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now_text(self) -> str:
        return _dt_text(self._now())

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 500) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _boundary_for_site(self, connection, *, actor, site_id: str):
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor["organization_id"] != site["organization_id"] and actor["role"] != "admin":
            raise PermissionDenied("不能操作其他组织的场所")
        return site

    def _load_boundary(self, connection, boundary_id: str):
        row = connection.execute(
            "SELECT * FROM retrofit_boundaries WHERE boundary_id=?", (boundary_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("设备边界不存在")
        return row

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True,
                                json.loads(row["response_json"]))
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now_text()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False, response)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now_text())

    # ------------------------------------------------------------------
    # 登记：设备边界、计量点、校准、批次、调整因子
    # ------------------------------------------------------------------

    def register_boundary(self, *, request_id: str, actor_id: str, boundary_id: str,
                          site_id: str, name: str, scope: dict[str, Any] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "boundary_id": boundary_id, "site_id": site_id,
                   "name": name, "scope": scope or {}}
        if not isinstance(scope or {}, dict):
            raise ValidationError("scope 必须是对象")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            self._boundary_for_site(conn, actor=actor, site_id=site_id)
            boundary_id = self._identifier(boundary_id, "boundary_id")
            name = self._text(name, "name")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO retrofit_boundaries(boundary_id,site_id,name,scope_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (boundary_id, site_id, name, canonical_json(scope or {}), actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("设备边界编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="retrofit.boundary.registered",
                            resource_type="retrofit_boundary", resource_id=boundary_id,
                            detail={"site_id": site_id, "name": name})
                return "retrofit_boundary", boundary_id, {"boundary_id": boundary_id}

            return self._idempotent(conn, request_id=request_id, action="retrofit_register_boundary",
                                    payload=payload, create=create)

    def register_meter(self, *, request_id: str, actor_id: str, meter_id: str, boundary_id: str,
                       meter_type: str, unit: str, min_range: float | None = None,
                       max_range: float | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "meter_id": meter_id, "boundary_id": boundary_id,
                   "meter_type": meter_type, "unit": unit, "min_range": min_range, "max_range": max_range}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            self._load_boundary(conn, boundary_id)
            meter_id = self._identifier(meter_id, "meter_id")
            unit = self._text(unit, "unit", 40)
            if meter_type not in METER_TYPES:
                raise ValidationError("meter_type 必须是 steam、output 或 energy")
            min_range, max_range = self._ranges(min_range, max_range)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO retrofit_meters(meter_id,boundary_id,meter_type,unit,min_range,max_range,"
                        "active,created_by,created_at) VALUES(?,?,?,?,?,?,1,?,?)",
                        (meter_id, boundary_id, meter_type, unit, min_range, max_range, actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("计量点编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="retrofit.meter.registered",
                            resource_type="retrofit_meter", resource_id=meter_id,
                            detail={"boundary_id": boundary_id, "meter_type": meter_type, "unit": unit})
                return "retrofit_meter", meter_id, {"meter_id": meter_id}

            return self._idempotent(conn, request_id=request_id, action="retrofit_register_meter",
                                    payload=payload, create=create)

    def _ranges(self, min_range: Any, max_range: Any) -> tuple[float | None, float | None]:
        if min_range is not None:
            min_range = float(min_range)
        if max_range is not None:
            max_range = float(max_range)
        if min_range is not None and max_range is not None and min_range > max_range:
            raise ValidationError("min_range 不能大于 max_range")
        return min_range, max_range

    def register_calibration(self, *, request_id: str, actor_id: str, calibration_id: str,
                             meter_id: str, valid_from: str, valid_until: str | None = None,
                             certificate_no: str | None = None) -> WriteReceipt:
        start = _parse_dt(valid_from, "valid_from")
        end = _parse_dt(valid_until, "valid_until") if valid_until else None
        if end and end < start:
            raise ValidationError("valid_until 不能早于 valid_from")
        payload = {"actor_id": actor_id, "calibration_id": calibration_id, "meter_id": meter_id,
                   "valid_from": _dt_text(start), "valid_until": _dt_text(end) if end else None,
                   "certificate_no": certificate_no}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            meter = conn.execute("SELECT * FROM retrofit_meters WHERE meter_id=?", (meter_id,)).fetchone()
            if meter is None:
                raise NotFoundError("计量点不存在")
            calibration_id = self._identifier(calibration_id, "calibration_id")
            certificate_no = self._text(certificate_no or calibration_id, "certificate_no", 120)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO retrofit_calibrations(calibration_id,meter_id,certificate_no,valid_from,"
                        "valid_until,revoked,created_by,created_at) VALUES(?,?,?,?,?,0,?,?)",
                        (calibration_id, meter_id, certificate_no, _dt_text(start),
                         _dt_text(end) if end else None, actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("校准记录编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="retrofit.calibration.registered",
                            resource_type="retrofit_calibration", resource_id=calibration_id,
                            detail={"meter_id": meter_id, "valid_from": _dt_text(start),
                                    "valid_until": _dt_text(end) if end else None})
                return "retrofit_calibration", calibration_id, {"calibration_id": calibration_id}

            return self._idempotent(conn, request_id=request_id, action="retrofit_register_calibration",
                                    payload=payload, create=create)

    def register_batch(self, *, request_id: str, actor_id: str, batch_id: str, boundary_id: str,
                       product_code: str, period_kind: str, shift_name: str, started_at: str,
                       ended_at: str, output: float, steam_unit_cost: float,
                       downtime_minutes: int = 0) -> WriteReceipt:
        start = _parse_dt(started_at, "started_at")
        end = _parse_dt(ended_at, "ended_at")
        if end <= start:
            raise ValidationError("ended_at 必须晚于 started_at")
        output = float(output)
        steam_unit_cost = float(steam_unit_cost)
        downtime_minutes = int(downtime_minutes)
        if output < 0 or steam_unit_cost < 0 or downtime_minutes < 0:
            raise ValidationError("产量、蒸汽单价和停机时长不能为负")
        if period_kind not in PERIOD_KINDS:
            raise ValidationError("period_kind 必须是 baseline 或 verification")
        payload = {"actor_id": actor_id, "batch_id": batch_id, "boundary_id": boundary_id,
                   "product_code": product_code, "period_kind": period_kind, "shift_name": shift_name,
                   "started_at": _dt_text(start), "ended_at": _dt_text(end), "output": output,
                   "steam_unit_cost": steam_unit_cost, "downtime_minutes": downtime_minutes}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            self._load_boundary(conn, boundary_id)
            batch_id = self._identifier(batch_id, "batch_id")
            product_code = self._identifier(product_code, "product_code")
            shift_name = self._text(shift_name, "shift_name", 80)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO retrofit_batches(batch_id,boundary_id,product_code,period_kind,shift_name,"
                        "started_at,ended_at,output,steam_unit_cost,downtime_minutes,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (batch_id, boundary_id, product_code, period_kind, shift_name, _dt_text(start),
                         _dt_text(end), output, steam_unit_cost, downtime_minutes, actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("生产批次编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="retrofit.batch.registered",
                            resource_type="retrofit_batch", resource_id=batch_id,
                            detail={"boundary_id": boundary_id, "period_kind": period_kind,
                                    "product_code": product_code, "shift_name": shift_name})
                return "retrofit_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(conn, request_id=request_id, action="retrofit_register_batch",
                                    payload=payload, create=create)

    def propose_adjustment_factor(self, *, request_id: str, actor_id: str, factor_id: str,
                                  boundary_id: str, period_kind: str, factor: float, reason: str,
                                  product_code: str | None = None) -> WriteReceipt:
        """工程人员登记用于归一化的调整因子（产品结构、停机时间等），需独立批准。"""

        factor = float(factor)
        if factor <= 0:
            raise ValidationError("调整因子必须为正数")
        if period_kind not in PERIOD_KINDS:
            raise ValidationError("period_kind 必须是 baseline 或 verification")
        reason = self._text(reason, "reason")
        product_code = self._identifier(product_code, "product_code") if product_code else None
        payload = {"actor_id": actor_id, "factor_id": factor_id, "boundary_id": boundary_id,
                   "period_kind": period_kind, "product_code": product_code, "factor": factor, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            self._load_boundary(conn, boundary_id)
            factor_id = self._identifier(factor_id, "factor_id")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO retrofit_factors(factor_id,boundary_id,product_code,period_kind,factor,reason,"
                        "status,proposed_by,created_at) VALUES(?,?,?,?,?,?,'proposed',?,?)",
                        (factor_id, boundary_id, product_code, period_kind, factor, reason,
                         actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("调整因子编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="retrofit.factor.proposed",
                            resource_type="retrofit_factor", resource_id=factor_id,
                            detail={"boundary_id": boundary_id, "period_kind": period_kind,
                                    "product_code": product_code, "factor": factor})
                return "retrofit_factor", factor_id, {"factor_id": factor_id, "status": "proposed"}

            return self._idempotent(conn, request_id=request_id, action="retrofit_propose_factor",
                                    payload=payload, create=create)

    def review_adjustment_factor(self, *, request_id: str, actor_id: str, factor_id: str,
                                 decision: str, note: str | None = None) -> WriteReceipt:
        """独立复核者批准或驳回调整因子，只有批准的因子才会进入核验计算。"""

        if decision not in ("approved", "rejected"):
            raise ValidationError("decision 必须是 approved 或 rejected")
        payload = {"actor_id": actor_id, "factor_id": factor_id, "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "reviewer")
            row = conn.execute("SELECT * FROM retrofit_factors WHERE factor_id=?", (factor_id,)).fetchone()
            if row is None:
                raise NotFoundError("调整因子不存在")
            if row["status"] != "proposed":
                raise ConflictError("调整因子已经复核")
            factor_id = row["factor_id"]

            def create():
                conn.execute(
                    "UPDATE retrofit_factors SET status=?,reviewed_by=?,reviewed_at=?,review_note=? "
                    "WHERE factor_id=?",
                    (decision, actor_id, self._now_text(), note or "", factor_id),
                )
                self._audit(conn, actor_id=actor_id, action=f"retrofit.factor.{decision}",
                            resource_type="retrofit_factor", resource_id=factor_id,
                            detail={"decision": decision, "note": note or ""})
                return "retrofit_factor", factor_id, {"factor_id": factor_id, "status": decision}

            return self._idempotent(conn, request_id=request_id, action="retrofit_review_factor",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 读数登记：缺失 / 异常 / 迟到
    # ------------------------------------------------------------------

    def record_reading(self, *, request_id: str, actor_id: str, meter_id: str, batch_id: str,
                       observed_at: str, value: float | None = None, note: str = "") -> WriteReceipt:
        observed = _parse_dt(observed_at, "observed_at")
        payload = {"actor_id": actor_id, "meter_id": meter_id, "batch_id": batch_id,
                   "observed_at": _dt_text(observed), "value": value, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            meter = conn.execute("SELECT * FROM retrofit_meters WHERE meter_id=?", (meter_id,)).fetchone()
            if meter is None:
                raise NotFoundError("计量点不存在")
            batch = conn.execute("SELECT * FROM retrofit_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                raise NotFoundError("生产批次不存在")
            if batch["boundary_id"] != meter["boundary_id"]:
                raise ValidationError("计量点与批次不属于同一设备边界")
            existing = conn.execute(
                "SELECT reading_id,status FROM retrofit_readings WHERE meter_id=? AND batch_id=?",
                (meter_id, batch_id),
            ).fetchone()
            if existing and not (existing["status"] == "missing" and value is not None):
                # recorded 读数不可改（快照可能已引用）；anomalous 须走校准/计量更正流程。
                raise ConflictError("该计量点在该批次上已有读数，不能重复登记")

            status, stored_value = self._classify_reading(meter=meter, value=value)
            if existing:
                reading_id = existing["reading_id"]

                def create():
                    conn.execute(
                        "UPDATE retrofit_readings SET value=?,status=?,observed_at=?,received_at=?,note=? "
                        "WHERE reading_id=?",
                        (stored_value, status, _dt_text(observed), self._now_text(), note or "", reading_id),
                    )
                    self._audit(conn, actor_id=actor_id, action="retrofit.reading.filled",
                                resource_type="retrofit_reading", resource_id=reading_id,
                                detail={"meter_id": meter_id, "batch_id": batch_id, "status": status,
                                        "value": stored_value})
                    return "retrofit_reading", reading_id, {"reading_id": reading_id, "status": status}
            else:
                reading_id = uuid.uuid4().hex

                def create():
                    conn.execute(
                        "INSERT INTO retrofit_readings(reading_id,meter_id,batch_id,value,status,observed_at,"
                        "received_at,note,created_by) VALUES(?,?,?,?,?,?,?,?,?)",
                        (reading_id, meter_id, batch_id, stored_value, status, _dt_text(observed),
                         self._now_text(), note or "", actor_id),
                    )
                    self._audit(conn, actor_id=actor_id, action="retrofit.reading.recorded",
                                resource_type="retrofit_reading", resource_id=reading_id,
                                detail={"meter_id": meter_id, "batch_id": batch_id, "status": status,
                                        "value": stored_value})
                    return "retrofit_reading", reading_id, {"reading_id": reading_id, "status": status}

            return self._idempotent(conn, request_id=request_id, action="retrofit_record_reading",
                                    payload=payload, create=create)

    def _classify_reading(self, *, meter, value) -> tuple[str, float | None]:
        """判定读数本身的质量状态：缺失或超量程异常。

        “迟到”不是读数的固有属性，而是它相对于某次冻结快照的关系：
        快照只捕获冻结时刻已登记的读数，之后到达的读数由 explain 派生标注，
        不改变任何既有快照，但可在重新冻结时被采纳。
        """

        if value is None:
            return "missing", None
        numeric = float(value)
        lo, hi = meter["min_range"], meter["max_range"]
        if (lo is not None and numeric < lo) or (hi is not None and numeric > hi):
            return "anomalous", numeric
        return "recorded", numeric

    # ------------------------------------------------------------------
    # 核验：冻结快照、替代计算、独立确认、关闭
    # ------------------------------------------------------------------

    def freeze_verification(self, *, request_id: str, actor_id: str, boundary_id: str, name: str,
                            baseline_start: str, baseline_end: str,
                            verification_start: str, verification_end: str,
                            parent_verification_id: str | None = None,
                            exclude_batches: set[str] | None = None,
                            alternate_id: str | None = None) -> WriteReceipt:
        """冻结一份核验数据快照并计算收益；替代计算被接受时生成子核验。"""

        windows = {
            "baseline_start": _dt_text(_parse_dt(baseline_start, "baseline_start")),
            "baseline_end": _dt_text(_parse_dt(baseline_end, "baseline_end")),
            "verification_start": _dt_text(_parse_dt(verification_start, "verification_start")),
            "verification_end": _dt_text(_parse_dt(verification_end, "verification_end")),
        }
        payload = {"actor_id": actor_id, "boundary_id": boundary_id, "name": name, **windows,
                   "parent_verification_id": parent_verification_id,
                   "exclude_batches": sorted(exclude_batches or []), "alternate_id": alternate_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            boundary = self._load_boundary(conn, boundary_id)
            if windows["baseline_end"] <= windows["baseline_start"]:
                raise ValidationError("基准期窗口无效")
            if windows["verification_end"] <= windows["verification_start"]:
                raise ValidationError("验证期窗口无效")
            if parent_verification_id:
                parent = conn.execute(
                    "SELECT * FROM retrofit_verifications WHERE verification_id=? AND boundary_id=?",
                    (parent_verification_id, boundary_id),
                ).fetchone()
                if parent is None:
                    raise NotFoundError("父核验不存在")
            name = self._text(name, "name", 120)
            excluded = {self._identifier(b, "exclude_batch") for b in (exclude_batches or set())}
            frozen_at = self._now_text()
            verification_id = uuid.uuid4().hex

            meters = conn.execute(
                "SELECT * FROM retrofit_meters WHERE boundary_id=? AND active=1 ORDER BY meter_id",
                (boundary_id,),
            ).fetchall()
            steam_meters = [m for m in meters if m["meter_type"] == "steam"]
            output_meters = [m for m in meters if m["meter_type"] == "output"]
            if not steam_meters:
                raise ValidationError("设备边界下没有在用的蒸汽计量点，无法核验")

            batches = self._window_batches(conn, boundary_id=boundary_id, windows=windows)
            snapshot = self._build_snapshot(
                conn, meters=meters, steam_meters=steam_meters, output_meters=output_meters,
                batches=batches, windows=windows, excluded=excluded, frozen_at=frozen_at,
                boundary_id=boundary_id, name=name,
            )
            sufficient = snapshot["baseline_included"] >= 2 and snapshot["verification_included"] >= 2
            results = self._compute_results(snapshot) if sufficient else None

            def create():
                conn.execute(
                    "INSERT INTO retrofit_verifications(verification_id,boundary_id,parent_verification_id,name,"
                    "baseline_start,baseline_end,verification_start,verification_end,frozen_at,frozen_by,status,"
                    "sufficient_data,results_json,snapshot_hash,alternate_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'frozen',?,?,?,?)",
                    (verification_id, boundary_id, parent_verification_id, name, windows["baseline_start"],
                     windows["baseline_end"], windows["verification_start"], windows["verification_end"],
                     frozen_at, actor_id, 1 if sufficient else 0,
                     canonical_json(results) if results else None, snapshot["hash"], alternate_id),
                )
                self._persist_snapshot(conn, verification_id=verification_id, snapshot=snapshot,
                                       meters=meters)
                if parent_verification_id:
                    conn.execute(
                        "UPDATE retrofit_verifications SET status='superseded' WHERE verification_id=?",
                        (parent_verification_id,),
                    )
                self._audit(conn, actor_id=actor_id, action="retrofit.verification.frozen",
                            resource_type="retrofit_verification", resource_id=verification_id,
                            detail={"boundary_id": boundary_id, "snapshot_hash": snapshot["hash"],
                                    "sufficient_data": sufficient, "parent_verification_id": parent_verification_id,
                                    "alternate_id": alternate_id,
                                    "included_batches": snapshot["included_batch_ids"],
                                    "excluded_batches": [e["batch_id"] for e in snapshot["exclusions"]]})
                response = {"verification_id": verification_id, "snapshot_hash": snapshot["hash"],
                            "sufficient_data": sufficient}
                return "retrofit_verification", verification_id, response

            receipt = self._idempotent(conn, request_id=request_id,
                                       action="retrofit_freeze_verification", payload=payload, create=create)
            return receipt

    def _window_batches(self, conn, *, boundary_id: str, windows: dict[str, str]):
        rows = conn.execute(
            "SELECT * FROM retrofit_batches WHERE boundary_id=? AND "
            "((period_kind='baseline' AND started_at>=? AND started_at<=?) OR "
            "(period_kind='verification' AND started_at>=? AND started_at<=?)) "
            "ORDER BY period_kind,started_at,batch_id",
            (boundary_id, windows["baseline_start"], windows["baseline_end"],
             windows["verification_start"], windows["verification_end"]),
        ).fetchall()
        return rows

    def _valid_calibration(self, conn, *, meter_id: str, at: datetime):
        text = _dt_text(at)
        return conn.execute(
            "SELECT * FROM retrofit_calibrations WHERE meter_id=? AND revoked=0 AND valid_from<=? "
            "AND (valid_until IS NULL OR valid_until>=?) ORDER BY valid_from DESC LIMIT 1",
            (meter_id, text, text),
        ).fetchone()

    def _approved_factor(self, conn, *, boundary_id: str, product_code: str, period_kind: str):
        row = conn.execute(
            "SELECT * FROM retrofit_factors WHERE boundary_id=? AND product_code=? AND period_kind=? "
            "AND status='approved' ORDER BY reviewed_at DESC LIMIT 1",
            (boundary_id, product_code, period_kind),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM retrofit_factors WHERE boundary_id=? AND product_code IS NULL "
                "AND period_kind=? AND status='approved' ORDER BY reviewed_at DESC LIMIT 1",
                (boundary_id, period_kind),
            ).fetchone()
        return row

    def _build_snapshot(self, conn, *, meters, steam_meters, output_meters, batches, windows,
                        excluded, frozen_at, boundary_id, name) -> dict[str, Any]:
        meter_rows = [{"meter_id": m["meter_id"], "meter_type": m["meter_type"], "unit": m["unit"],
                       "min_range": m["min_range"], "max_range": m["max_range"]} for m in meters]
        batch_entries: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        exclusions: list[dict[str, Any]] = []
        included_ids: list[str] = []
        quality = {"missing": [], "anomalous": [], "late": []}

        for batch in batches:
            reasons: list[str] = []
            steam_total = 0.0
            steam_ok = True
            batch_sources: list[dict[str, Any]] = []
            for meter in steam_meters:
                reading = conn.execute(
                    "SELECT * FROM retrofit_readings WHERE meter_id=? AND batch_id=?",
                    (meter["meter_id"], batch["batch_id"]),
                ).fetchone()
                calibration = self._valid_calibration(conn, meter_id=meter["meter_id"], at=_parse_dt(batch["started_at"], "started_at"))
                source, ok, reason = self._reading_source(batch=batch, meter=meter, reading=reading,
                                                          calibration=calibration)
                batch_sources.append(source)
                sources.append(source)
                if not ok:
                    steam_ok = False
                    reasons.append(reason)
                elif source["value"] is not None:
                    steam_total += float(source["value"])

            output_value = float(batch["output"])
            if output_meters:
                output_total = 0.0
                output_ok = True
                for meter in output_meters:
                    reading = conn.execute(
                        "SELECT * FROM retrofit_readings WHERE meter_id=? AND batch_id=?",
                        (meter["meter_id"], batch["batch_id"]),
                    ).fetchone()
                    calibration = self._valid_calibration(conn, meter_id=meter["meter_id"],
                                                          at=_parse_dt(batch["started_at"], "started_at"))
                    source, ok, reason = self._reading_source(batch=batch, meter=meter, reading=reading,
                                                              calibration=calibration)
                    batch_sources.append(source)
                    sources.append(source)
                    if not ok:
                        output_ok = False
                        reasons.append(reason)
                    elif source["value"] is not None:
                        output_total += float(source["value"])
                if output_ok and output_total > 0:
                    output_value = output_total
                elif output_ok:
                    reasons.append("产量计量读数合计为零")

            energy_meters = [m for m in meters if m["meter_type"] == "energy"]
            for meter in energy_meters:
                reading = conn.execute(
                    "SELECT * FROM retrofit_readings WHERE meter_id=? AND batch_id=?",
                    (meter["meter_id"], batch["batch_id"]),
                ).fetchone()
                calibration = self._valid_calibration(conn, meter_id=meter["meter_id"],
                                                      at=_parse_dt(batch["started_at"], "started_at"))
                source, _ok, _reason = self._reading_source(batch=batch, meter=meter, reading=reading,
                                                            calibration=calibration)
                batch_sources.append(source)
                sources.append(source)

            for source in batch_sources:
                if source["reading_status"] in quality:
                    quality[source["reading_status"]].append(
                        {"batch_id": batch["batch_id"], "meter_id": source["meter_id"],
                         "reading_id": source["reading_id"]})

            factor = self._approved_factor(conn, boundary_id=boundary_id,
                                           product_code=batch["product_code"],
                                           period_kind=batch["period_kind"])
            factor_value = float(factor["factor"]) if factor else 1.0
            adjusted_rate = (steam_total * factor_value / output_value) if output_value > 0 else None

            if batch["batch_id"] in excluded:
                reasons.append("按替代计算提案排除")
            if steam_ok and steam_total <= 0:
                reasons.append("蒸汽消耗合计为零")
            if output_value <= 0:
                reasons.append("产量为零")

            include = not reasons
            entry = {
                "batch_id": batch["batch_id"], "period_kind": batch["period_kind"],
                "product_code": batch["product_code"], "shift_name": batch["shift_name"],
                "started_at": batch["started_at"], "ended_at": batch["ended_at"],
                "steam_total": round(steam_total, 9), "output": round(output_value, 9),
                "steam_unit_cost": float(batch["steam_unit_cost"]),
                "downtime_minutes": int(batch["downtime_minutes"]),
                "factor_id": factor["factor_id"] if factor else None,
                "factor_value": factor_value,
                "adjusted_rate": round(adjusted_rate, 9) if adjusted_rate is not None else None,
                "included": include, "exclusion_reason": None if include else "; ".join(reasons),
            }
            batch_entries.append(entry)
            if include:
                included_ids.append(batch["batch_id"])
            else:
                exclusions.append({"batch_id": batch["batch_id"], "period_kind": batch["period_kind"],
                                   "reasons": reasons})

        included = [e for e in batch_entries if e["included"]]
        base_inc = [e for e in included if e["period_kind"] == "baseline"]
        ver_inc = [e for e in included if e["period_kind"] == "verification"]
        snapshot = {
            "frozen_at": frozen_at, "boundary_id": boundary_id, "name": name, "windows": windows,
            "meters": meter_rows, "batches": sorted(batch_entries, key=lambda e: (e["period_kind"], e["started_at"], e["batch_id"])),
            "sources": sorted(sources, key=lambda s: (s["batch_id"], s["meter_id"])),
            "included_batch_ids": sorted(included_ids),
            "exclusions": exclusions, "data_quality": quality,
            "baseline_included": len(base_inc), "verification_included": len(ver_inc),
        }
        snapshot["hash"] = digest(self._hashable_snapshot(snapshot))
        return snapshot

    def _reading_source(self, *, batch, meter, reading, calibration) -> tuple[dict[str, Any], bool, str]:
        """把一条读数（含缺失与异常）转换为可追溯的快照来源。"""

        if reading is None:
            source = {
                "batch_id": batch["batch_id"], "meter_id": meter["meter_id"],
                "reading_id": f"missing:{meter['meter_id']}:{batch['batch_id']}",
                "value": None, "reading_status": "missing", "observed_at": batch["started_at"],
                "calibration_id": calibration["calibration_id"] if calibration else None,
                "calibration_valid": 1 if calibration else 0,
            }
            return source, False, f"计量点 {meter['meter_id']} 缺少读数"
        source = {
            "batch_id": batch["batch_id"], "meter_id": meter["meter_id"],
            "reading_id": reading["reading_id"], "value": reading["value"],
            "reading_status": reading["status"], "observed_at": reading["observed_at"],
            "calibration_id": calibration["calibration_id"] if calibration else None,
            "calibration_valid": 1 if calibration else 0,
        }
        if reading["status"] != "recorded":
            label = {"missing": "读数缺失", "anomalous": "读数超量程异常", "late": "读数迟到"}[reading["status"]]
            return source, False, f"计量点 {meter['meter_id']} {label}"
        if calibration is None:
            return source, False, f"计量点 {meter['meter_id']} 校准时点无有效校准"
        return source, True, ""

    def _hashable_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """抽取被冻结的字段；运行期计数与质量分组都由明细派生。"""

        return {
            "frozen_at": snapshot["frozen_at"], "boundary_id": snapshot["boundary_id"],
            "name": snapshot["name"], "windows": snapshot["windows"],
            "meters": snapshot["meters"], "batches": snapshot["batches"],
            "sources": snapshot["sources"],
        }

    def _compute_results(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        measurements = []
        for entry in snapshot["batches"]:
            if not entry["included"]:
                continue
            measurements.append(ValidMeasurement(
                batch_id=entry["batch_id"], meter_id=",".join(
                    sorted(m["meter_id"] for m in snapshot["meters"] if m["meter_type"] == "steam")),
                product_code=entry["product_code"], period_kind=entry["period_kind"],
                steam=entry["steam_total"], output=entry["output"],
                steam_unit_cost=entry["steam_unit_cost"], adjustment_factor=entry["factor_value"],
            ))
        try:
            result = calculate_savings(measurements)
        except ValueError:
            return None
        output_total = result.total_output
        return {
            "baseline_count": result.baseline_count,
            "verification_count": result.verification_count,
            "baseline_rate": round(result.baseline_rate, 9),
            "verification_rate": round(result.verification_rate, 9),
            "rate_delta": round(result.rate_delta, 9),
            "unit_cost_baseline": round(result.unit_cost_baseline, 9),
            "unit_cost_verification": round(result.unit_cost_verification, 9),
            "unit_cost_delta": round(result.unit_cost_delta, 9),
            "total_output": round(output_total, 9),
            "savings_steam": round(result.savings_steam, 9),
            "savings_amount": round(result.savings_amount, 9),
            "confidence_level": result.confidence_level,
            "margin_of_error": round(result.margin_of_error, 9),
            "rate_delta_low": round(result.rate_delta_low, 9),
            "rate_delta_high": round(result.rate_delta_high, 9),
            "savings_steam_low": round(-result.rate_delta_high * output_total, 9),
            "savings_steam_high": round(-result.rate_delta_low * output_total, 9),
            "method": result.method,
            "effective_batch_ids": sorted(m.batch_id for m in measurements),
        }

    def _persist_snapshot(self, conn, *, verification_id: str, snapshot: dict[str, Any], meters) -> None:
        for meter in meters:
            conn.execute(
                "INSERT INTO retrofit_verification_meters(verification_id,meter_id,meter_type,unit,min_range,max_range) "
                "VALUES(?,?,?,?,?,?)",
                (verification_id, meter["meter_id"], meter["meter_type"], meter["unit"],
                 meter["min_range"], meter["max_range"]),
            )
        for entry in snapshot["batches"]:
            conn.execute(
                "INSERT INTO retrofit_verification_batches(verification_id,batch_id,period_kind,product_code,"
                "steam_total,output,steam_unit_cost,factor_id,factor_value,adjusted_rate,included,exclusion_reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (verification_id, entry["batch_id"], entry["period_kind"], entry["product_code"],
                 entry["steam_total"], entry["output"], entry["steam_unit_cost"], entry["factor_id"],
                 entry["factor_value"], entry["adjusted_rate"], 1 if entry["included"] else 0,
                 entry["exclusion_reason"]),
            )
        for source in snapshot["sources"]:
            conn.execute(
                "INSERT INTO retrofit_verification_sources(verification_id,batch_id,meter_id,reading_id,value,"
                "reading_status,observed_at,calibration_id,calibration_valid) VALUES(?,?,?,?,?,?,?,?,?)",
                (verification_id, source["batch_id"], source["meter_id"], source["reading_id"],
                 source["value"], source["reading_status"], source["observed_at"],
                 source["calibration_id"], source["calibration_valid"]),
            )

    def propose_alternate(self, *, request_id: str, actor_id: str, verification_id: str,
                          excluded_batches: list[str], rationale: str) -> WriteReceipt:
        """工程人员对冻结核验提出替代计算（剔除特定批次），等待独立复核。"""

        if not excluded_batches:
            raise ValidationError("替代计算必须至少说明一个被剔除的批次")
        rationale = self._text(rationale, "rationale")
        excluded = sorted({self._identifier(b, "excluded_batch") for b in excluded_batches})
        payload = {"actor_id": actor_id, "verification_id": verification_id,
                   "excluded_batches": excluded, "rationale": rationale}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            verification = conn.execute(
                "SELECT * FROM retrofit_verifications WHERE verification_id=?", (verification_id,)
            ).fetchone()
            if verification is None:
                raise NotFoundError("核验不存在")
            if verification["status"] not in ("frozen", "calibration_hold"):
                raise ConflictError("只能对未确认的冻结核验提出替代计算")
            known = {r["batch_id"] for r in conn.execute(
                "SELECT batch_id FROM retrofit_verification_batches WHERE verification_id=?", (verification_id,))}
            unknown = [b for b in excluded if b not in known]
            if unknown:
                raise ValidationError(f"批次不在核验快照中：{', '.join(unknown)}")
            alternate_id = uuid.uuid4().hex

            def create():
                conn.execute(
                    "INSERT INTO retrofit_alternates(alternate_id,verification_id,proposed_by,"
                    "excluded_batches_json,rationale,status,created_at) VALUES(?,?,?,?,?,'proposed',?)",
                    (alternate_id, verification_id, actor_id, canonical_json(excluded),
                     rationale, self._now_text()),
                )
                self._audit(conn, actor_id=actor_id, action="retrofit.alternate.proposed",
                            resource_type="retrofit_alternate", resource_id=alternate_id,
                            detail={"verification_id": verification_id, "excluded_batches": excluded})
                return "retrofit_alternate", alternate_id, {"alternate_id": alternate_id}

            return self._idempotent(conn, request_id=request_id, action="retrofit_propose_alternate",
                                    payload=payload, create=create)

    def review_alternate(self, *, request_id: str, actor_id: str, alternate_id: str,
                         decision: str, note: str = "", name: str | None = None) -> WriteReceipt:
        """独立复核者接受替代计算时，用相同窗口冻结一份子核验。"""

        if decision not in ("accepted", "rejected"):
            raise ValidationError("decision 必须是 accepted 或 rejected")
        payload = {"actor_id": actor_id, "alternate_id": alternate_id, "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "reviewer")
            alternate = conn.execute(
                "SELECT * FROM retrofit_alternates WHERE alternate_id=?", (alternate_id,)
            ).fetchone()
            if alternate is None:
                raise NotFoundError("替代计算提案不存在")
            if alternate["status"] != "proposed":
                raise ConflictError("替代计算提案已经复核")
            verification = conn.execute(
                "SELECT * FROM retrofit_verifications WHERE verification_id=?",
                (alternate["verification_id"],),
            ).fetchone()
            excluded = set(json.loads(alternate["excluded_batches_json"]))
            child_id = uuid.uuid4().hex if decision == "accepted" else None

            def create():
                conn.execute(
                    "UPDATE retrofit_alternates SET status=?,reviewed_by=?,reviewed_at=?,review_note=?,"
                    "child_verification_id=? WHERE alternate_id=?",
                    (decision, actor_id, self._now_text(), note, child_id, alternate_id),
                )
                detail = {"verification_id": alternate["verification_id"], "decision": decision}
                if decision == "accepted":
                    # 在同一事务内用相同窗口与排除清单冻结子核验。
                    self._freeze_locked(
                        conn, verification_id=child_id, actor=actor,
                        boundary_id=verification["boundary_id"],
                        name=name or f"{verification['name']}（替代计算）",
                        baseline_start=verification["baseline_start"],
                        baseline_end=verification["baseline_end"],
                        verification_start=verification["verification_start"],
                        verification_end=verification["verification_end"],
                        parent_verification_id=verification["verification_id"],
                        exclude_batches=excluded, alternate_id=alternate_id,
                    )
                    detail["child_verification_id"] = child_id
                self._audit(conn, actor_id=actor_id, action=f"retrofit.alternate.{decision}",
                            resource_type="retrofit_alternate", resource_id=alternate_id, detail=detail)
                return "retrofit_alternate", alternate_id, {
                    "alternate_id": alternate_id, "status": decision,
                    "child_verification_id": child_id}

            return self._idempotent(conn, request_id=request_id, action="retrofit_review_alternate",
                                    payload=payload, create=create)

    def _freeze_locked(self, conn, *, verification_id: str, actor, boundary_id: str, name: str,
                       baseline_start: str, baseline_end: str, verification_start: str,
                       verification_end: str, parent_verification_id: str | None,
                       exclude_batches: set[str], alternate_id: str | None) -> str:
        """在已持有事务时执行冻结，供替代计算接受复用。"""

        windows = {"baseline_start": baseline_start, "baseline_end": baseline_end,
                   "verification_start": verification_start, "verification_end": verification_end}
        meters = conn.execute(
            "SELECT * FROM retrofit_meters WHERE boundary_id=? AND active=1 ORDER BY meter_id",
            (boundary_id,),
        ).fetchall()
        steam_meters = [m for m in meters if m["meter_type"] == "steam"]
        output_meters = [m for m in meters if m["meter_type"] == "output"]
        if not steam_meters:
            raise ValidationError("设备边界下没有在用的蒸汽计量点，无法核验")
        batches = self._window_batches(conn, boundary_id=boundary_id, windows=windows)
        frozen_at = self._now_text()
        snapshot = self._build_snapshot(
            conn, meters=meters, steam_meters=steam_meters, output_meters=output_meters,
            batches=batches, windows=windows, excluded=set(exclude_batches), frozen_at=frozen_at,
            boundary_id=boundary_id, name=name,
        )
        sufficient = snapshot["baseline_included"] >= 2 and snapshot["verification_included"] >= 2
        results = self._compute_results(snapshot) if sufficient else None
        conn.execute(
            "INSERT INTO retrofit_verifications(verification_id,boundary_id,parent_verification_id,name,"
            "baseline_start,baseline_end,verification_start,verification_end,frozen_at,frozen_by,status,"
            "sufficient_data,results_json,snapshot_hash,alternate_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'frozen',?,?,?,?)",
            (verification_id, boundary_id, parent_verification_id, name, baseline_start, baseline_end,
             verification_start, verification_end, frozen_at, actor["actor_id"],
             1 if sufficient else 0, canonical_json(results) if results else None,
             snapshot["hash"], alternate_id),
        )
        self._persist_snapshot(conn, verification_id=verification_id, snapshot=snapshot, meters=meters)
        conn.execute("UPDATE retrofit_verifications SET status='superseded' WHERE verification_id=?",
                     (parent_verification_id,))
        self._audit(conn, actor_id=actor["actor_id"], action="retrofit.verification.frozen",
                    resource_type="retrofit_verification", resource_id=verification_id,
                    detail={"boundary_id": boundary_id, "snapshot_hash": snapshot["hash"],
                            "sufficient_data": sufficient, "parent_verification_id": parent_verification_id,
                            "alternate_id": alternate_id,
                            "included_batches": snapshot["included_batch_ids"],
                            "excluded_batches": [e["batch_id"] for e in snapshot["exclusions"]]})
        return verification_id

    def confirm_verification(self, *, request_id: str, actor_id: str, verification_id: str,
                             note: str = "") -> WriteReceipt:
        """独立复核者确认最终收益；工程角色不能确认自己的核验。"""

        payload = {"actor_id": actor_id, "verification_id": verification_id, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "reviewer")
            row = conn.execute("SELECT * FROM retrofit_verifications WHERE verification_id=?",
                               (verification_id,)).fetchone()
            if row is None:
                raise NotFoundError("核验不存在")

            def create():
                if row["status"] == "calibration_hold":
                    raise ConflictError("核验因校准失效暂停，不能确认；请在恢复计量后重新冻结")
                if row["status"] != "frozen":
                    raise ConflictError("只有已冻结且未确认的核验可以确认")
                if not row["sufficient_data"]:
                    raise ConflictError("有效测量不足，置信边界不可用，不能确认收益")
                conn.execute(
                    "UPDATE retrofit_verifications SET status='confirmed',confirmed_by=?,confirmed_at=?,"
                    "review_note=? WHERE verification_id=?",
                    (actor_id, self._now_text(), note, verification_id),
                )
                results = json.loads(row["results_json"]) if row["results_json"] else None
                self._audit(conn, actor_id=actor_id, action="retrofit.verification.confirmed",
                            resource_type="retrofit_verification", resource_id=verification_id,
                            detail={"snapshot_hash": row["snapshot_hash"],
                                    "savings_steam": results and results["savings_steam"],
                                    "savings_amount": results and results["savings_amount"]})
                return "retrofit_verification", verification_id, {
                    "verification_id": verification_id, "status": "confirmed"}

            return self._idempotent(conn, request_id=request_id, action="retrofit_confirm_verification",
                                    payload=payload, create=create)

    def close_verification(self, *, request_id: str, actor_id: str, verification_id: str,
                           note: str = "") -> WriteReceipt:
        """关闭已确认的期间；关闭后只能通过更正记录披露影响。"""

        payload = {"actor_id": actor_id, "verification_id": verification_id, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "reviewer")
            row = conn.execute("SELECT * FROM retrofit_verifications WHERE verification_id=?",
                               (verification_id,)).fetchone()
            if row is None:
                raise NotFoundError("核验不存在")

            def create():
                if row["status"] != "confirmed":
                    raise ConflictError("只有已确认的核验可以关闭")
                conn.execute(
                    "UPDATE retrofit_verifications SET status='closed',closed_by=?,closed_at=? WHERE verification_id=?",
                    (actor_id, self._now_text(), verification_id),
                )
                self._audit(conn, actor_id=actor_id, action="retrofit.verification.closed",
                            resource_type="retrofit_verification", resource_id=verification_id,
                            detail={"snapshot_hash": row["snapshot_hash"]})
                return "retrofit_verification", verification_id, {
                    "verification_id": verification_id, "status": "closed"}

            return self._idempotent(conn, request_id=request_id, action="retrofit_close_verification",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 校准失效：暂停未结算收益，更正披露已关闭期间
    # ------------------------------------------------------------------

    def report_calibration_failure(self, *, request_id: str, actor_id: str, meter_id: str,
                                   effective_from: str, reason: str,
                                   calibration_id: str | None = None) -> WriteReceipt:
        """登记校准失效，定位受影响的核验结论并执行暂停或更正披露。"""

        effective = _parse_dt(effective_from, "effective_from")
        reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "meter_id": meter_id, "effective_from": _dt_text(effective),
                   "reason": reason, "calibration_id": calibration_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "reviewer", "auditor")
            meter = conn.execute("SELECT * FROM retrofit_meters WHERE meter_id=?", (meter_id,)).fetchone()
            if meter is None:
                raise NotFoundError("计量点不存在")
            if calibration_id is None:
                coverage = conn.execute(
                    "SELECT * FROM retrofit_calibrations WHERE meter_id=? AND revoked=0 AND valid_from<=? "
                    "ORDER BY valid_from DESC LIMIT 1",
                    (meter_id, _dt_text(effective)),
                ).fetchone()
                calibration_id = coverage["calibration_id"] if coverage else None
            elif not conn.execute("SELECT 1 FROM retrofit_calibrations WHERE calibration_id=? AND meter_id=?",
                                  (calibration_id, meter_id)).fetchone():
                raise NotFoundError("校准记录不存在或不属于该计量点")
            correction_id = uuid.uuid4().hex

            def create():
                conn.execute(
                    "INSERT INTO retrofit_corrections(correction_id,meter_id,calibration_id,effective_from,"
                    "reason,reported_by,reported_at,status) VALUES(?,?,?,?,?,?,?,'open')",
                    (correction_id, meter_id, calibration_id, _dt_text(effective), reason,
                     actor_id, self._now_text()),
                )
                if calibration_id:
                    conn.execute("UPDATE retrofit_calibrations SET revoked=1 WHERE calibration_id=?",
                                 (calibration_id,))
                impacts = self._apply_calibration_impact(
                    conn, correction_id=correction_id, meter=meter, effective=effective)
                self._audit(conn, actor_id=actor_id, action="retrofit.calibration.failure_reported",
                            resource_type="retrofit_correction", resource_id=correction_id,
                            detail={"meter_id": meter_id, "calibration_id": calibration_id,
                                    "effective_from": _dt_text(effective), "impacts": impacts})
                return "retrofit_correction", correction_id, {
                    "correction_id": correction_id, "impacts": impacts}

            return self._idempotent(conn, request_id=request_id, action="retrofit_calibration_failure",
                                    payload=payload, create=create)

    def _apply_calibration_impact(self, conn, *, correction_id: str, meter, effective: datetime) -> list[dict[str, Any]]:
        """定位引用该计量点、观测时点落在失效日之后的快照来源。"""

        effective_text = _dt_text(effective)
        impacted_sources = conn.execute(
            "SELECT vs.verification_id, vs.reading_id, vs.batch_id, vs.value, v.status, v.results_json "
            "FROM retrofit_verification_sources vs "
            "JOIN retrofit_verifications v ON v.verification_id=vs.verification_id "
            "JOIN retrofit_verification_batches vb ON vb.verification_id=vs.verification_id "
            "AND vb.batch_id=vs.batch_id AND vb.included=1 "
            "WHERE vs.meter_id=? AND vs.calibration_valid=1 AND vs.reading_status='recorded' "
            "AND vs.observed_at>=?",
            (meter["meter_id"], effective_text),
        ).fetchall()
        by_verification: dict[str, list[Any]] = {}
        for row in impacted_sources:
            by_verification.setdefault(row["verification_id"], []).append(row)
        impacts: list[dict[str, Any]] = []
        for verification_id, rows in by_verification.items():
            status = rows[0]["status"]
            results = json.loads(rows[0]["results_json"]) if rows[0]["results_json"] else {}
            at_risk = float(results.get("savings_amount", 0.0) or 0.0)
            measurement_ids = [r["reading_id"] for r in rows]
            if status == "closed":
                action, prior_status = "disclosure", "closed"
            elif status in ("frozen", "confirmed"):
                action, prior_status = "hold", status
                conn.execute(
                    "UPDATE retrofit_verifications SET status='calibration_hold',hold_since=?,hold_reason=? "
                    "WHERE verification_id=?",
                    (self._now_text(), f"计量点 {meter['meter_id']} 校准失效，待重新计量", verification_id),
                )
            elif status == "calibration_hold":
                # 已经暂停：只补充影响记录，不重复改写暂停时间。
                action, prior_status = "hold", status
            else:
                # superseded（已被替代计算取代）：没有可结算收益，仅披露影响。
                action, prior_status = "disclosure", status
            conn.execute(
                "INSERT INTO retrofit_impacts(correction_id,verification_id,action,prior_status,"
                "affected_measurements_json,savings_amount_at_risk,detail_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (correction_id, verification_id, action, prior_status,
                 canonical_json(measurement_ids), at_risk,
                 canonical_json({"meter_id": meter["meter_id"],
                                 "batches": sorted({r["batch_id"] for r in rows})}),
                 self._now_text()),
            )
            impacts.append({"verification_id": verification_id, "action": action,
                            "prior_status": prior_status, "savings_amount_at_risk": at_risk,
                            "affected_readings": len(measurement_ids)})
        return sorted(impacts, key=lambda item: item["verification_id"])

    def clear_correction(self, *, request_id: str, actor_id: str, correction_id: str,
                         note: str = "") -> WriteReceipt:
        """标记更正已处理（例如新校准生效）；不改变既有暂停与披露结论。"""

        payload = {"actor_id": actor_id, "correction_id": correction_id, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "reviewer", "auditor")
            row = conn.execute("SELECT * FROM retrofit_corrections WHERE correction_id=?",
                               (correction_id,)).fetchone()
            if row is None:
                raise NotFoundError("更正记录不存在")

            def create():
                conn.execute("UPDATE retrofit_corrections SET status='cleared' WHERE correction_id=?",
                             (correction_id,))
                self._audit(conn, actor_id=actor_id, action="retrofit.correction.cleared",
                            resource_type="retrofit_correction", resource_id=correction_id,
                            detail={"note": note})
                return "retrofit_correction", correction_id, {
                    "correction_id": correction_id, "status": "cleared"}

            return self._idempotent(conn, request_id=request_id, action="retrofit_clear_correction",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询与解释
    # ------------------------------------------------------------------

    def get_verification(self, verification_id: str) -> dict[str, Any]:
        conn = self.database.connection
        row = conn.execute("SELECT * FROM retrofit_verifications WHERE verification_id=?",
                           (verification_id,)).fetchone()
        if row is None:
            raise NotFoundError("核验不存在")
        return self._verification_view(conn, row)

    def list_verifications(self, boundary_id: str) -> list[dict[str, Any]]:
        conn = self.database.connection
        rows = conn.execute(
            "SELECT * FROM retrofit_verifications WHERE boundary_id=? ORDER BY frozen_at,verification_id",
            (boundary_id,),
        ).fetchall()
        return [{k: row[k] for k in ("verification_id", "name", "status", "frozen_at",
                                      "sufficient_data", "snapshot_hash", "parent_verification_id")}
                for row in rows]

    def _verification_view(self, conn, row) -> dict[str, Any]:
        batches = [dict(r) for r in conn.execute(
            "SELECT * FROM retrofit_verification_batches WHERE verification_id=? "
            "ORDER BY period_kind,product_code,batch_id", (row["verification_id"],))]
        sources = [dict(r) for r in conn.execute(
            "SELECT * FROM retrofit_verification_sources WHERE verification_id=? "
            "ORDER BY batch_id,meter_id", (row["verification_id"],))]
        meters = [dict(r) for r in conn.execute(
            "SELECT meter_id,meter_type,unit,min_range,max_range FROM retrofit_verification_meters "
            "WHERE verification_id=? ORDER BY meter_id", (row["verification_id"],))]
        results = json.loads(row["results_json"]) if row["results_json"] else None
        corrections = [dict(r) for r in conn.execute(
            "SELECT i.* FROM retrofit_impacts i WHERE i.verification_id=? ORDER BY i.created_at",
            (row["verification_id"],))]
        for item in corrections:
            item["affected_measurements"] = json.loads(item.pop("affected_measurements_json"))
            item["detail"] = json.loads(item.pop("detail_json"))
        return {
            "verification_id": row["verification_id"], "boundary_id": row["boundary_id"],
            "parent_verification_id": row["parent_verification_id"], "name": row["name"],
            "windows": {"baseline_start": row["baseline_start"], "baseline_end": row["baseline_end"],
                        "verification_start": row["verification_start"], "verification_end": row["verification_end"]},
            "status": row["status"], "frozen_at": row["frozen_at"], "frozen_by": row["frozen_by"],
            "sufficient_data": bool(row["sufficient_data"]), "snapshot_hash": row["snapshot_hash"],
            "confirmed_by": row["confirmed_by"], "confirmed_at": row["confirmed_at"],
            "closed_by": row["closed_by"], "closed_at": row["closed_at"],
            "hold_since": row["hold_since"], "hold_reason": row["hold_reason"],
            "results": results, "meters": meters, "batches": batches, "sources": sources,
            "calibration_impacts": corrections,
        }

    def explain(self, verification_id: str) -> dict[str, Any]:
        """解释节省量、单位成本变化和置信边界分别来自哪些有效测量。"""

        view = self.get_verification(verification_id)
        results = view["results"]
        effective = []
        for batch in view["batches"]:
            if not batch["included"]:
                continue
            contributing_sources = [
                {"meter_id": s["meter_id"], "reading_id": s["reading_id"], "value": s["value"],
                 "calibration_id": s["calibration_id"]}
                for s in view["sources"]
                if s["batch_id"] == batch["batch_id"] and s["reading_status"] == "recorded"
                and s["calibration_valid"] and s["meter_id"] in
                {m["meter_id"] for m in view["meters"] if m["meter_type"] in ("steam", "output")}
            ]
            effective.append({
                "batch_id": batch["batch_id"], "period_kind": batch["period_kind"],
                "product_code": batch["product_code"], "steam_total": batch["steam_total"],
                "output": batch["output"], "steam_unit_cost": batch["steam_unit_cost"],
                "adjustment_factor": batch["factor_value"], "factor_id": batch["factor_id"],
                "adjusted_rate": batch["adjusted_rate"], "sources": contributing_sources,
            })
        missing = [s for s in view["sources"] if s["reading_status"] == "missing"]
        anomalous = [s for s in view["sources"] if s["reading_status"] == "anomalous"]
        # 迟到读数在查询时派生：冻结之后才登记/补到、而快照在该（批次,计量点）上没有采用有效读数。
        conn = self.database.connection
        late_rows = conn.execute(
            "SELECT r.reading_id,r.meter_id,r.batch_id,r.value,r.status,r.received_at,r.observed_at "
            "FROM retrofit_readings r "
            "JOIN retrofit_verification_meters vm ON vm.verification_id=? AND vm.meter_id=r.meter_id "
            "JOIN retrofit_verification_batches vb ON vb.verification_id=? AND vb.batch_id=r.batch_id "
            "JOIN retrofit_verification_sources vs ON vs.verification_id=? "
            "AND vs.meter_id=r.meter_id AND vs.batch_id=r.batch_id "
            "WHERE r.status='recorded' AND r.received_at > ? AND vs.reading_status <> 'recorded'",
            (verification_id, verification_id, verification_id, view["frozen_at"]),
        ).fetchall()
        late = [dict(r) for r in late_rows]
        calibrations = [
            {"meter_id": s["meter_id"], "calibration_id": s["calibration_id"],
             "observed_at": s["observed_at"]}
            for s in view["sources"]
            if s["reading_status"] == "recorded" and s["calibration_valid"]
        ]
        explanation: dict[str, Any] = {
            "verification_id": verification_id, "status": view["status"],
            "snapshot_hash": view["snapshot_hash"], "frozen_at": view["frozen_at"],
            "windows": view["windows"], "sufficient_data": view["sufficient_data"],
            "results": results,
            "effective_measurements": effective,
            "excluded_batches": [
                {"batch_id": b["batch_id"], "period_kind": b["period_kind"],
                 "reason": b["exclusion_reason"]}
                for b in view["batches"] if not b["included"]],
            "data_quality": {
                "missing": [{"batch_id": s["batch_id"], "meter_id": s["meter_id"],
                             "reading_id": s["reading_id"]} for s in missing],
                "anomalous": [{"batch_id": s["batch_id"], "meter_id": s["meter_id"],
                               "reading_id": s["reading_id"], "value": s["value"]} for s in anomalous],
                "late": [{"batch_id": r["batch_id"], "meter_id": r["meter_id"],
                          "reading_id": r["reading_id"], "value": r["value"],
                          "current_status": r["status"], "received_at": r["received_at"]}
                         for r in late],
            },
            "late_reading_note": "迟到读数在本快照冻结之后才登记，未参与本次计算且不改变快照；"
                                 "重新冻结核验时若读数有效则可被采纳",
            "calibrations_relied_on": [
                {"meter_id": meter_id, "calibration_id": calibration_id}
                for meter_id, calibration_id in sorted(
                    {(c["meter_id"], c["calibration_id"]) for c in calibrations if c["calibration_id"]},
                    key=lambda pair: (pair[0], pair[1] or ""))],
            "calibration_impacts": view["calibration_impacts"],
        }
        if results:
            explanation["confidence"] = {
                "level": results["confidence_level"], "method": results["method"],
                "margin_of_error": results["margin_of_error"],
                "rate_delta_interval": [results["rate_delta_low"], results["rate_delta_high"]],
                "savings_steam_interval": [results["savings_steam_low"], results["savings_steam_high"]],
                "basis": "仅使用快照中状态为 recorded、校准在观测时点有效且未被替代计算排除的批次测量；"
                         "调整后的批次单位蒸汽采用 Welch 双样本 t 区间估计两期均值之差",
            }
            explanation["derivation"] = {
                "rate_delta": "verification_rate - baseline_rate（经批准因子调整，负值表示单位蒸汽下降）",
                "savings_steam": "-rate_delta × 验证期总产量",
                "savings_amount": "-(验证期单位蒸汽成本-基准期单位蒸汽成本) × 验证期总产量",
                "unit_cost": "单位蒸汽（吨/百升）×蒸汽单价（元/吨）= 元/百升",
            }
        else:
            explanation["confidence"] = None
        return explanation
