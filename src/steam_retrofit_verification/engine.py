"""节能收益计算与读数质量分类的纯函数引擎。

引擎只依赖快照内已经冻结的数据结构，不访问数据库与时钟，因此任何历史核验
都可以用其快照原样复算。计算口径：

- 单位蒸汽成本（强度）= 周期内有效蒸汽量 / 有效合格产品产量（kg/升）；
- 期望验证期蒸汽量 = 基准期强度 × 批准调整因子 × 验证期产量；
- 节省量 = 期望蒸汽量 − 实际蒸汽量；
- 节省量置信边界使用两周期批次强度均值差的 Welch-t 区间。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .models import (
    READING_ANOMALOUS,
    READING_LATE,
    READING_MISSING,
    READING_OK,
    MeasurementEvidence,
    SavingsResult,
)

MAD_K = 3.5  # 中位数绝对偏差离群阈值（约 3σ 的稳健替代）


@dataclass(frozen=True)
class ClassifiedBatch:
    """冻结时刻对一个批次的判定结果。"""

    batch_id: str
    period: str
    product_code: str
    started_at: str
    ended_at: str
    output: float
    meter_id: str | None
    steam_kg: float | None
    measured_at: str | None
    received_at: str | None
    reading_state: str
    quality_reason: str
    overridden_steam_kg: float | None = None


def _calibration_valid(calibrations: list[dict[str, Any]], measured_at: str | None) -> bool:
    """测量时刻是否被有效校准证书覆盖。

    取测量时刻之前签发的最新一份证书，测量时刻不晚于其有效期截止日即有效；
    重新校准会刷新有效期，旧证书过期不影响新证书覆盖的测量。
    """

    if not measured_at:
        return False
    prior = [c for c in calibrations if c["certified_at"] <= measured_at]
    if not prior:
        return False
    latest = max(prior, key=lambda c: (c["certified_at"], c.get("seq", 0)))
    return measured_at <= latest["valid_until"]


def classify_snapshot_entries(
    *,
    batches: list[dict[str, Any]],
    readings: list[dict[str, Any]],
    calibrations_by_meter: dict[str, list[dict[str, Any]]],
    active_issue_meter_windows: list[tuple[str, str, str]],
    cutoff_at: str,
    overrides: dict[str, float],
) -> list[ClassifiedBatch]:
    """把批次与读数在给定截止时刻分类为 ok/missing/anomalous/late。

    readings 中保留同一批次的多次上报历史；截止时刻前取最新一次，
    截止时刻之后才到达的上报判为迟到。
    """

    entries: list[ClassifiedBatch] = []
    for batch in batches:
        batch_id = batch["batch_id"]
        history = sorted(
            (r for r in readings if r["batch_id"] == batch_id),
            key=lambda r: r["received_at"],
        )
        in_time = [r for r in history if r["received_at"] <= cutoff_at]
        late = [r for r in history if r["received_at"] > cutoff_at]
        latest = in_time[-1] if in_time else None

        if latest is None:
            if late:
                state, reason = READING_LATE, "读数在快照截止之后才到达"
                entries.append(ClassifiedBatch(
                    batch_id, batch["period"], batch["product_code"], batch["started_at"],
                    batch["ended_at"], float(batch["output"]), late[-1]["meter_id"],
                    float(late[-1]["steam_kg"]), late[-1]["measured_at"], late[-1]["received_at"],
                    state, reason, overrides.get(batch_id)))
            else:
                entries.append(ClassifiedBatch(
                    batch_id, batch["period"], batch["product_code"], batch["started_at"],
                    batch["ended_at"], float(batch["output"]), None, None, None, None,
                    READING_MISSING, "截止时刻前没有任何蒸汽读数", overrides.get(batch_id)))
            continue

        steam = float(latest["steam_kg"])
        meter_id = latest["meter_id"]
        measured_at = latest["measured_at"]
        reason = ""
        state = READING_OK
        if steam <= 0:
            state, reason = READING_ANOMALOUS, "蒸汽读数必须为正数"
        elif float(batch["output"]) <= 0:
            state, reason = READING_ANOMALOUS, "合格产量必须为正数"
        elif not _calibration_valid(calibrations_by_meter.get(meter_id, []), measured_at):
            state, reason = READING_ANOMALOUS, "测量时刻计量点校准证书失效"
        else:
            for issue_meter, issue_from, issue_to in active_issue_meter_windows:
                if issue_meter == meter_id and issue_from <= measured_at <= issue_to:
                    state, reason = READING_ANOMALOUS, "测量时刻处于已报告的计量失效窗口"
                    break
        entries.append(ClassifiedBatch(
            batch_id, batch["period"], batch["product_code"], batch["started_at"],
            batch["ended_at"], float(batch["output"]), meter_id, steam,
            measured_at, latest["received_at"], state, reason or "通过质量与校准校验",
            overrides.get(batch_id)))

    _flag_intensity_outliers(entries)
    return entries


def _flag_intensity_outliers(entries: list[ClassifiedBatch]) -> None:
    """在每个周期内用 MAD 规则把强度离群批次改判为异常。"""

    for period in ("baseline", "verification"):
        candidates = [e for e in entries if e.period == period
                      and e.reading_state == READING_OK and e.steam_kg and e.output > 0]
        if len(candidates) < 4:
            continue
        intensities = sorted(e.steam_kg / e.output for e in candidates)
        median = intensities[len(intensities) // 2]
        deviations = sorted(abs(x - median) for x in intensities)
        mad = deviations[len(deviations) // 2]
        if mad <= 0:
            continue
        for entry in candidates:
            z = 0.6745 * (entry.steam_kg / entry.output - median) / mad
            if abs(z) > MAD_K:
                object.__setattr__(entry, "reading_state", READING_ANOMALOUS)
                object.__setattr__(entry, "quality_reason",
                                   f"单位蒸汽强度偏离中位数 {z:.1f} 个稳健标准差")


def _inverse_t(p: float, df: float) -> float:
    """t 分位数的 Cornish-Fisher 近似（标准库无 beta 反函数）。"""

    z = inverse_normal(p)
    if df <= 0 or not math.isfinite(df):
        return z
    t = z + (z ** 3 + z) / (4 * df) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * df ** 2)
    return t


def inverse_normal(p: float) -> float:
    """标准正态分位数（Acklam 有理逼近）。"""

    if not 0.0 < p < 1.0:
        raise ValueError("概率必须位于 (0,1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > p_high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sample_variance(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


def compute_savings(
    entries: list[ClassifiedBatch],
    factors: dict[str, float],
    *,
    method: str,
    confidence_level: float = 0.95,
) -> SavingsResult:
    """依据分类后的批次计算节省量、单位成本变化与置信边界。"""

    def rows(period: str) -> list[ClassifiedBatch]:
        return [e for e in entries if e.period == period]

    def steam_of(entry: ClassifiedBatch) -> float:
        # 替代值只能顶替缺失/异常批次；有效测量永远不被静默替换。
        if entry.overridden_steam_kg is not None and entry.reading_state != READING_OK:
            return entry.overridden_steam_kg
        return entry.steam_kg or 0.0

    def effective(entry: ClassifiedBatch) -> bool:
        # 标准口径只认真实有效测量；工程替代口径允许用批准的替代值
        # 顶替缺失/异常批次，但迟到读数绝不进入冻结快照的计算。
        if entry.reading_state == READING_OK:
            return True
        return (method == "engineering_alternative"
                and entry.overridden_steam_kg is not None
                and entry.reading_state in (READING_MISSING, READING_ANOMALOUS))

    baseline = rows("baseline")
    verification = rows("verification")
    base_ok = [e for e in baseline if effective(e)]
    ver_ok = [e for e in verification if effective(e)]

    factor_product = 1.0
    for value in factors.values():
        factor_product *= value

    base_steam = sum(steam_of(e) for e in base_ok)
    base_output = sum(e.output for e in base_ok)
    ver_steam = sum(steam_of(e) for e in ver_ok)
    ver_output = sum(e.output for e in ver_ok)
    base_intensity = base_steam / base_output if base_output else 0.0
    ver_intensity = ver_steam / ver_output if ver_output else 0.0
    adjusted_base_intensity = base_intensity * factor_product

    expected_steam = adjusted_base_intensity * ver_output
    saved = expected_steam - ver_steam

    base_x = [steam_of(e) / e.output for e in base_ok if e.output > 0]
    ver_x = [steam_of(e) / e.output for e in ver_ok if e.output > 0]
    bm, vm = _mean(base_x), _mean(ver_x)
    bvar = _sample_variance(base_x, bm)
    vvar = _sample_variance(ver_x, vm)
    se_components = bvar / len(base_x) + vvar / len(ver_x) if base_x and ver_x else 0.0
    se = math.sqrt(se_components) if se_components > 0 else 0.0
    if se > 0 and bvar and vvar:
        df = se_components ** 2 / ((bvar / len(base_x)) ** 2 / (len(base_x) - 1)
                                   + (vvar / len(ver_x)) ** 2 / (len(ver_x) - 1))
        tcrit = _inverse_t(0.5 + confidence_level / 2, df)
    else:
        tcrit = 0.0
    margin_intensity = tcrit * se * factor_product
    change = adjusted_base_intensity - ver_intensity
    lower = (change - margin_intensity) * ver_output
    upper = (change + margin_intensity) * ver_output

    total_output = sum(e.output for e in verification) + sum(e.output for e in baseline)
    ok_output = sum(e.output for e in base_ok) + sum(e.output for e in ver_ok)
    missing = sum(1 for e in entries if e.reading_state == READING_MISSING)
    anomalous = sum(1 for e in entries if e.reading_state == READING_ANOMALOUS)
    late = sum(1 for e in entries if e.reading_state == READING_LATE)

    evidence = _build_evidence(entries, base_ok, ver_ok, method)
    return SavingsResult(
        method=method,
        baseline_unit_cost=base_intensity,
        verification_unit_cost=ver_intensity,
        unit_cost_change=change,
        expected_steam_kg=expected_steam,
        actual_steam_kg=ver_steam,
        steam_saved_kg=saved,
        energy_saved_ratio=saved / expected_steam if expected_steam else 0.0,
        baseline_n=len(base_ok),
        verification_n=len(ver_ok),
        missing_count=missing,
        anomalous_count=anomalous,
        late_count=late,
        coverage_ratio=ok_output / total_output if total_output else 0.0,
        confidence_level=confidence_level if se > 0 else 0.0,
        margin_of_error=margin_intensity * ver_output,
        interval_lower=lower,
        interval_upper=upper,
        adjustments=dict(factors),
        evidence=evidence,
    )


def _build_evidence(entries: list[ClassifiedBatch], base_ok=None, ver_ok=None,
                    method: str = "standard") -> list[MeasurementEvidence]:
    """每个计量点列出其贡献的批次及是否被纳入计算。"""

    included_ids = {e.batch_id for e in (base_ok or [])} | {e.batch_id for e in (ver_ok or [])}
    grouped: dict[str, list[ClassifiedBatch]] = {}
    meterless: list[ClassifiedBatch] = []
    for entry in entries:
        if entry.meter_id:
            grouped.setdefault(entry.meter_id, []).append(entry)
        else:
            meterless.append(entry)
    evidence: list[MeasurementEvidence] = []
    for meter_id, items in sorted(grouped.items()):
        included_items = [i for i in items if i.batch_id in included_ids]
        excluded_items = [i for i in items if i.batch_id not in included_ids]
        if included_items and not excluded_items:
            state, included, note = READING_OK, True, "该计量点全部批次作为有效测量纳入计算"
        elif included_items:
            state, included = "partial", True
            note = (f"{len(included_items)} 个批次纳入"
                    f"{'（含工程替代值）' if method == 'engineering_alternative' else ''}，"
                    f"{len(excluded_items)} 个批次因缺失/异常/迟到被剔除")
        else:
            state, included = next(iter({i.reading_state for i in items})), False
            note = "该计量点没有可纳入的有效测量"
        evidence.append(MeasurementEvidence(
            metric="steam", meter_id=meter_id,
            batch_ids=[i.batch_id for i in items],
            reading_state=state, included=included, note=note,
        ))
    if meterless:
        evidence.append(MeasurementEvidence(
            metric="steam", meter_id="",
            batch_ids=[i.batch_id for i in meterless],
            reading_state=READING_MISSING, included=False,
            note="这些批次没有任何计量点读数，未纳入计算",
        ))
    return evidence


def result_to_dict(result: SavingsResult) -> dict[str, Any]:
    return {
        "method": result.method,
        "baseline_unit_cost": result.baseline_unit_cost,
        "verification_unit_cost": result.verification_unit_cost,
        "unit_cost_change": result.unit_cost_change,
        "expected_steam_kg": result.expected_steam_kg,
        "actual_steam_kg": result.actual_steam_kg,
        "steam_saved_kg": result.steam_saved_kg,
        "energy_saved_ratio": result.energy_saved_ratio,
        "baseline_n": result.baseline_n,
        "verification_n": result.verification_n,
        "missing_count": result.missing_count,
        "anomalous_count": result.anomalous_count,
        "late_count": result.late_count,
        "coverage_ratio": result.coverage_ratio,
        "confidence_level": result.confidence_level,
        "margin_of_error": result.margin_of_error,
        "interval_lower": result.interval_lower,
        "interval_upper": result.interval_upper,
        "adjustments": result.adjustments,
        "evidence": [e.__dict__ for e in result.evidence],
    }


def result_from_dict(data: dict[str, Any]) -> SavingsResult:
    evidence = [MeasurementEvidence(**e) for e in data.get("evidence", [])]
    return SavingsResult(
        method=data["method"],
        baseline_unit_cost=data["baseline_unit_cost"],
        verification_unit_cost=data["verification_unit_cost"],
        unit_cost_change=data["unit_cost_change"],
        expected_steam_kg=data["expected_steam_kg"],
        actual_steam_kg=data["actual_steam_kg"],
        steam_saved_kg=data["steam_saved_kg"],
        energy_saved_ratio=data["energy_saved_ratio"],
        baseline_n=data["baseline_n"],
        verification_n=data["verification_n"],
        missing_count=data["missing_count"],
        anomalous_count=data["anomalous_count"],
        late_count=data["late_count"],
        coverage_ratio=data["coverage_ratio"],
        confidence_level=data["confidence_level"],
        margin_of_error=data["margin_of_error"],
        interval_lower=data["interval_lower"],
        interval_upper=data["interval_upper"],
        adjustments=data.get("adjustments", {}),
        evidence=evidence,
    )
