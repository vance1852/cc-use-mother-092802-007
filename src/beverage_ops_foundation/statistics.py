"""基于有效测量的节能计算，仅依赖 Python 标准库。

所有输入都必须是已经通过数据质量判定的“有效测量”；
缺失、异常和迟到读数由上层服务过滤，不进入这里的计算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

# 学生 t 分布小样本临界值（双尾 95%），缺失自由度时回退到正态分布。
# 仅收录自由度 1..30 与常用档，覆盖两条产线的小样本批次比较。
_T_CRIT_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
    7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179,
    13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101,
    19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064,
    25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}
_Z_95 = 1.96


@dataclass(frozen=True)
class ValidMeasurement:
    """进入计算的单条有效测量（批次级）。"""

    batch_id: str
    meter_id: str
    product_code: str
    period_kind: str          # baseline 或 verification
    steam: float              # 蒸汽消耗量（吨）
    output: float             # 产品产量（百升）
    steam_unit_cost: float    # 蒸汽单价（元/吨）
    adjustment_factor: float  # 已批准的调整因子（1 表示不调整）


@dataclass(frozen=True)
class SavingsResult:
    """描述一次核验计算的全部数值结论。"""

    baseline_count: int
    verification_count: int
    baseline_rate: float       # 基准期单位蒸汽（吨/百升）
    verification_rate: float   # 验证期单位蒸汽（吨/百升）
    rate_delta: float          # 单位蒸汽变化（验证-基准，负值为下降）
    unit_cost_baseline: float  # 基准期单位蒸汽成本（元/百升）
    unit_cost_verification: float
    unit_cost_delta: float     # 单位成本变化（元/百升，负值为下降）
    total_output: float        # 验证期总产量
    savings_steam: float       # 节省蒸汽（吨）
    savings_amount: float      # 节省金额（元）
    margin_of_error: float     # 单位蒸汽差的置信半宽（吨/百升）
    rate_delta_low: float      # 单位蒸汽差置信下界
    rate_delta_high: float     # 单位蒸汽差置信上界
    confidence_level: float
    method: str


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _variance(values: Sequence[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


def _t_critical(df: float) -> float:
    if df <= 0 or math.isinf(df):
        return _Z_95
    df_key = int(math.ceil(df))
    if df_key <= 0:
        return _Z_95
    if df_key >= 30:
        return _Z_95
    return _T_CRIT_95[df_key]


def adjusted_rate(m: ValidMeasurement) -> float:
    """单批次经批准因子调整后的单位蒸汽。

    基准期因子把产品结构、停机时间等批准的归一化口径应用到基准批次；
    验证期因子通常为 1。调整后的基准率代表“若按验证期口径生产”的水平。
    """

    return (m.steam * m.adjustment_factor) / m.output


def calculate_savings(measurements: Sequence[ValidMeasurement],
                      confidence_level: float = 0.95) -> SavingsResult:
    """根据有效测量计算节省量、单位成本变化与置信边界。

    采用 Welch 双样本 t 区间估计两期单位蒸汽均值之差，
    不假设两组方差相等，适合基准期与验证期批次数量不同的场景。
    """

    baseline = [m for m in measurements if m.period_kind == "baseline"]
    verification = [m for m in measurements if m.period_kind == "verification"]
    if len(baseline) < 1 or len(verification) < 1:
        raise ValueError("基准期与验证期都至少需要一条有效测量")
    if confidence_level != 0.95:
        raise ValueError("当前仅支持 95% 置信水平")

    base_rates = [adjusted_rate(m) for m in baseline]
    ver_rates = [adjusted_rate(m) for m in verification]
    base_mean = _mean(base_rates)
    ver_mean = _mean(ver_rates)
    rate_delta = ver_mean - base_mean

    base_var = _variance(base_rates, base_mean)
    ver_var = _variance(ver_rates, ver_mean)
    nb, nv = len(baseline), len(verification)
    standard_error = math.sqrt(base_var / nb + ver_var / nv)

    if standard_error > 0:
        denom = (base_var / nb) ** 2 / (nb - 1) + (ver_var / nv) ** 2 / (nv - 1)
        df = (base_var / nb + ver_var / nv) ** 2 / denom if denom > 0 else float("inf")
        t_crit = _t_critical(df)
        margin = t_crit * standard_error
    else:
        margin = 0.0

    base_unit_cost = _mean([m.steam_unit_cost * r for m, r in zip(baseline, base_rates)])
    ver_unit_cost = _mean([m.steam_unit_cost * r for m, r in zip(verification, ver_rates)])
    unit_cost_delta = ver_unit_cost - base_unit_cost

    total_output = sum(m.output for m in verification)
    savings_steam = -rate_delta * total_output
    savings_amount = -unit_cost_delta * total_output

    return SavingsResult(
        baseline_count=nb,
        verification_count=nv,
        baseline_rate=base_mean,
        verification_rate=ver_mean,
        rate_delta=rate_delta,
        unit_cost_baseline=base_unit_cost,
        unit_cost_verification=ver_unit_cost,
        unit_cost_delta=unit_cost_delta,
        total_output=total_output,
        savings_steam=savings_steam,
        savings_amount=savings_amount,
        margin_of_error=margin,
        rate_delta_low=rate_delta - margin,
        rate_delta_high=rate_delta + margin,
        confidence_level=confidence_level,
        method="welch_t_rate_delta_on_approved_adjusted_measurements",
    )
