"""技改收益核验领域使用的不可变数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 读数进入系统时的分类。
READING_OK = "ok"                # 通过质量校验的有效测量
READING_MISSING = "missing"      # 应有而未上报的读数
READING_ANOMALOUS = "anomalous"  # 已上报但未通过质量规则
READING_LATE = "late"            # 快照冻结之后才到达

READING_STATES = frozenset({READING_OK, READING_MISSING, READING_ANOMALOUS, READING_LATE})

# 核验生命周期。
VERIFICATION_DRAFT = "draft"            # 工程人员编制中
VERIFICATION_SUBMITTED = "submitted"    # 已提交独立复核
VERIFICATION_CONFIRMED = "confirmed"    # 独立复核者确认，收益成立
VERIFICATION_REJECTED = "rejected"      # 复核驳回，工程可修改重提
VERIFICATION_SUSPENDED = "suspended"    # 计量失效波及，未结算收益暂停
VERIFICATION_CLOSED = "closed"          # 期间关闭，此后只能更正披露
VERIFICATION_STATUSES = frozenset({
    VERIFICATION_DRAFT, VERIFICATION_SUBMITTED, VERIFICATION_CONFIRMED,
    VERIFICATION_REJECTED, VERIFICATION_SUSPENDED, VERIFICATION_CLOSED,
})

# 复核结论。
REVIEW_APPROVE = "approve"
REVIEW_REJECT = "reject"
REVIEW_RESUBMIT = "resubmit"  # 驳回后工程修改再次提交


@dataclass(frozen=True)
class EquipmentBoundary:
    """技改覆盖的设备边界：边界内计量点构成核验口径。"""

    boundary_id: str
    site_id: str
    name: str
    description: str
    meter_ids: tuple[str, ...]
    version: int
    created_by: str
    created_at: str


@dataclass(frozen=True)
class MeterPoint:
    """蒸汽/产量计量点及其校准有效期。"""

    meter_id: str
    boundary_id: str
    name: str
    metric: str            # steam | product
    unit: str
    calibration_due: str   # ISO 日期，校准证书有效期截止日（含）
    active: bool
    version: int


@dataclass(frozen=True)
class CalibrationRecord:
    """一次校准登记，延长或更新计量点校准有效期。"""

    calibration_id: str
    meter_id: str
    certified_at: str
    valid_until: str
    certificate_ref: str


@dataclass(frozen=True)
class ProductionBatch:
    """一个生产批次及其合格产量；周期在核验快照中按时间窗口派生。"""

    batch_id: str
    boundary_id: str
    product_code: str
    started_at: str
    ended_at: str
    output: float          # 合格产品产量（升）


@dataclass(frozen=True)
class AdjustmentFactor:
    """经批准的调整因子（如产品结构、负荷、停机折算）。"""

    factor_id: str
    boundary_id: str
    code: str
    value: float
    reason: str
    approved_by: str
    approved_at: str
    active: bool


@dataclass(frozen=True)
class Snapshot:
    """一次核验冻结的数据快照。

    读数接收时间晚于 frozen_at 的上报不会进入快照（判为迟到的依据），
    因此快照内容不可变且可用 payload_hash 复算校验。
    """

    snapshot_id: str
    verification_id: str
    boundary_id: str
    frozen_at: str
    baseline_start: str
    baseline_end: str
    verification_start: str
    verification_end: str
    payload: dict[str, Any]
    payload_hash: str


@dataclass(frozen=True)
class MeasurementEvidence:
    """解释结果数字来自哪些有效测量。"""

    metric: str
    meter_id: str
    batch_ids: list[str]
    reading_state: str
    included: bool
    note: str = ""


@dataclass(frozen=True)
class SavingsResult:
    """核验计算结果及置信边界。"""

    method: str
    baseline_unit_cost: float
    verification_unit_cost: float
    unit_cost_change: float
    expected_steam_kg: float
    actual_steam_kg: float
    steam_saved_kg: float
    energy_saved_ratio: float
    baseline_n: int
    verification_n: int
    missing_count: int
    anomalous_count: int
    late_count: int
    coverage_ratio: float
    confidence_level: float
    margin_of_error: float
    interval_lower: float
    interval_upper: float
    adjustments: dict[str, float] = field(default_factory=dict)
    evidence: list[MeasurementEvidence] = field(default_factory=list)


@dataclass(frozen=True)
class Verification:
    """一次技改收益核验单。"""

    verification_id: str
    boundary_id: str
    period: str
    title: str
    status: str
    method: str                  # standard | engineering_alternative
    engineering_rationale: str
    proposed_by: str
    proposed_at: str
    submitted_at: str | None
    confirmed_by: str | None
    confirmed_at: str | None
    snapshot_id: str | None
    result: SavingsResult | None
    settle_state: str            # unsettled | settled
    closed_at: str | None


@dataclass(frozen=True)
class ReviewDecision:
    """独立复核记录。"""

    review_id: str
    verification_id: str
    decision: str
    reviewer_id: str
    comment: str
    decided_at: str


@dataclass(frozen=True)
class CorrectionRecord:
    """期间关闭后披露计量问题影响的更正记录（只披露，不重算已结算收益）。"""

    correction_id: str
    verification_id: str
    reason: str
    impacted_meter_ids: list[str]
    disclosed_impact: dict[str, Any]
    recorded_by: str
    recorded_at: str
