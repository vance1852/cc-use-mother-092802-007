import unittest

from beverage_ops_foundation.statistics import (
    ValidMeasurement,
    adjusted_rate,
    calculate_savings,
)


def m(batch_id, kind, steam, output, cost=100.0, factor=1.0, product="P1"):
    return ValidMeasurement(batch_id=batch_id, meter_id="steam-1", product_code=product,
                            period_kind=kind, steam=steam, output=output,
                            steam_unit_cost=cost, adjustment_factor=factor)


class StatisticsTest(unittest.TestCase):
    def test_constant_rates_have_zero_margin(self):
        # 两期单位蒸汽完全一致：差值与置信半宽都为 0。
        data = [m("B1", "baseline", 100, 100), m("B2", "baseline", 110, 110),
                m("V1", "verification", 90, 100), m("V2", "verification", 99, 110)]
        result = calculate_savings(data)
        self.assertEqual(1.0, result.baseline_rate)
        self.assertEqual(0.9, result.verification_rate)
        self.assertAlmostEqual(-0.1, result.rate_delta)
        self.assertEqual(0.0, result.margin_of_error)
        self.assertEqual(result.rate_delta_low, result.rate_delta_high)
        # 节省 = 0.1 吨/百升 × 验证期产量 210
        self.assertAlmostEqual(21.0, result.savings_steam)

    def test_unit_cost_change_uses_steam_price(self):
        # 验证期蒸汽单价更高，节能金额小于蒸汽节省量按基准价折算的值。
        data = [m("B1", "baseline", 120, 100, cost=100.0),
                m("B2", "baseline", 120, 100, cost=100.0),
                m("V1", "verification", 90, 100, cost=120.0),
                m("V2", "verification", 90, 100, cost=120.0)]
        result = calculate_savings(data)
        self.assertEqual(120.0, result.unit_cost_baseline)
        self.assertEqual(108.0, result.unit_cost_verification)
        self.assertAlmostEqual(-12.0, result.unit_cost_delta)
        self.assertAlmostEqual(2400.0, result.savings_amount)

    def test_confidence_interval_covers_delta_for_varied_data(self):
        base = [m(f"B{i}", "baseline", 100 + 5 * i, 100) for i in range(6)]
        ver = [m(f"V{i}", "verification", 80 + 3 * i, 100) for i in range(6)]
        result = calculate_savings(base + ver)
        # 置信区间必须包含点估计且半宽为正。
        self.assertGreater(result.margin_of_error, 0)
        self.assertLess(result.rate_delta_low, result.rate_delta)
        self.assertGreater(result.rate_delta_high, result.rate_delta)
        # 区间上界仍低于零：整条区间支持“单位蒸汽下降”的结论。
        self.assertLess(result.rate_delta_high, 0)

    def test_adjustment_factor_scales_baseline_rate(self):
        measurement = m("B1", "baseline", 100, 100, factor=0.9)
        self.assertAlmostEqual(0.9, adjusted_rate(measurement))

    def test_requires_both_periods(self):
        with self.assertRaises(ValueError):
            calculate_savings([m("B1", "baseline", 100, 100)])
        with self.assertRaises(ValueError):
            calculate_savings([m("V1", "verification", 90, 100)])

    def test_only_95_percent_supported(self):
        with self.assertRaises(ValueError):
            calculate_savings([m("B1", "baseline", 100, 100),
                               m("V1", "verification", 90, 100)], confidence_level=0.90)


if __name__ == "__main__":
    unittest.main()
