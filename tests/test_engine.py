import unittest

from steam_retrofit_verification.engine import (
    classify_snapshot_entries,
    compute_savings,
    inverse_normal,
)


def calibration(meter="m1", start="2025-01-01T00:00:00Z", end="2027-01-01T00:00:00Z"):
    return [{"certified_at": start, "valid_until": end, "certificate_ref": "CERT-1"}]


def batch(bid, period, output=1000.0):
    return {"batch_id": bid, "period": period, "product_code": "P1",
            "started_at": "2026-06-02T00:00:00Z", "ended_at": "2026-06-03T00:00:00Z",
            "output": output}


def reading(bid, steam, *, meter="m1", measured="2026-06-02T12:00:00Z", received="2026-06-04T00:00:00Z"):
    return {"reading_id": f"r-{bid}", "batch_id": bid, "meter_id": meter, "steam_kg": steam,
            "measured_at": measured, "received_at": received}


class ClassifyTest(unittest.TestCase):
    CUTOFF = "2026-08-01T00:00:00Z"

    def test_missing_when_no_reading(self):
        entries = classify_snapshot_entries(
            batches=[batch("b1", "baseline")], readings=[],
            calibrations_by_meter={"m1": calibration()},
            active_issue_meter_windows=[], cutoff_at=self.CUTOFF, overrides={})
        self.assertEqual("missing", entries[0].reading_state)

    def test_late_when_only_reading_arrives_after_cutoff(self):
        entries = classify_snapshot_entries(
            batches=[batch("b1", "baseline")],
            readings=[reading("b1", 900, received="2026-08-02T00:00:00Z")],
            calibrations_by_meter={"m1": calibration()},
            active_issue_meter_windows=[], cutoff_at=self.CUTOFF, overrides={})
        self.assertEqual("late", entries[0].reading_state)
        self.assertEqual(900.0, entries[0].steam_kg)

    def test_anomalous_when_steam_non_positive(self):
        entries = classify_snapshot_entries(
            batches=[batch("b1", "baseline")],
            readings=[reading("b1", 0)],
            calibrations_by_meter={"m1": calibration()},
            active_issue_meter_windows=[], cutoff_at=self.CUTOFF, overrides={})
        self.assertEqual("anomalous", entries[0].reading_state)

    def test_anomalous_when_calibration_expired_at_measurement(self):
        entries = classify_snapshot_entries(
            batches=[batch("b1", "baseline")],
            readings=[reading("b1", 900, measured="2026-06-02T12:00:00Z")],
            calibrations_by_meter={"m1": calibration(end="2026-05-01T00:00:00Z")},
            active_issue_meter_windows=[], cutoff_at=self.CUTOFF, overrides={})
        self.assertEqual("anomalous", entries[0].reading_state)
        self.assertIn("校准", entries[0].quality_reason)

    def test_anomalous_inside_meter_issue_window(self):
        entries = classify_snapshot_entries(
            batches=[batch("b1", "baseline")],
            readings=[reading("b1", 900)],
            calibrations_by_meter={"m1": calibration()},
            active_issue_meter_windows=[("m1", "2026-06-01T00:00:00Z", "2026-06-10T00:00:00Z")],
            cutoff_at=self.CUTOFF, overrides={})
        self.assertEqual("anomalous", entries[0].reading_state)

    def test_latest_reading_before_cutoff_wins(self):
        entries = classify_snapshot_entries(
            batches=[batch("b1", "baseline")],
            readings=[reading("b1", 100, received="2026-06-04T00:00:00Z"),
                      reading("b1", 950, received="2026-06-05T00:00:00Z"),
                      reading("b1", 999, received="2026-09-01T00:00:00Z")],
            calibrations_by_meter={"m1": calibration()},
            active_issue_meter_windows=[], cutoff_at=self.CUTOFF, overrides={})
        self.assertEqual("ok", entries[0].reading_state)
        self.assertEqual(950.0, entries[0].steam_kg)


class ComputeTest(unittest.TestCase):
    def _entries(self, base_values, ver_values):
        batches = [batch(f"b{i}", "baseline") for i in range(len(base_values))] + \
                  [batch(f"v{i}", "verification") for i in range(len(ver_values))]
        readings = [reading(f"b{i}", v, measured="2026-06-02T12:00:00Z",
                            received="2026-06-10T00:00:00Z") for i, v in enumerate(base_values)]
        readings += [reading(f"v{i}", v, measured="2026-07-02T12:00:00Z",
                             received="2026-07-10T00:00:00Z") for i, v in enumerate(ver_values)]
        return classify_snapshot_entries(
            batches=batches, readings=readings,
            calibrations_by_meter={"m1": calibration()},
            active_issue_meter_windows=[],
            cutoff_at="2026-08-01T00:00:00Z", overrides={})

    def test_savings_and_confidence_interval(self):
        entries = self._entries(
            [1000, 1020, 980, 1010, 990],      # 基准强度约 1.0
            [850, 870, 840, 860, 880])        # 验证强度约 0.86
        result = compute_savings(entries, {}, method="standard", confidence_level=0.95)
        self.assertEqual(5, result.baseline_n)
        self.assertEqual(5, result.verification_n)
        self.assertGreater(result.steam_saved_kg, 0)
        self.assertAlmostEqual(0.14, result.energy_saved_ratio, delta=0.02)
        self.assertGreater(result.margin_of_error, 0)
        self.assertLess(result.interval_lower, result.steam_saved_kg)
        self.assertGreater(result.interval_upper, result.steam_saved_kg)
        self.assertEqual(0.95, result.confidence_level)

    def test_adjustment_factor_scales_expected_steam(self):
        entries = self._entries([1000, 1000, 1000, 1000, 1000],
                                [850, 850, 850, 850, 850])
        plain = compute_savings(entries, {}, method="standard")
        adjusted = compute_savings(entries, {"product_mix": 1.05}, method="standard")
        self.assertGreater(adjusted.expected_steam_kg, plain.expected_steam_kg)
        self.assertGreater(adjusted.steam_saved_kg, plain.steam_saved_kg)

    def test_engineering_override_recovers_anomalous_batch(self):
        import dataclasses

        entries = self._entries([1000, 1000, 1000, 1000, 1000],
                                [850, 850, 0, 850, 850])
        standard = compute_savings(entries, {}, method="standard")
        self.assertEqual(1, standard.anomalous_count)
        self.assertEqual(4, standard.verification_n)
        replaced = [dataclasses.replace(e, overridden_steam_kg=860.0)
                    if e.batch_id == "v2" else e for e in entries]
        alternative = compute_savings(replaced, {}, method="engineering_alternative")
        self.assertEqual(5, alternative.verification_n)
        self.assertGreater(alternative.verification_n, standard.verification_n)
        # 有效测量永远不被替代值替换：给正常批次配替代值不改变结果。
        guarded = [dataclasses.replace(e, overridden_steam_kg=1.0)
                   if e.batch_id == "v0" else e for e in entries]
        guarded_result = compute_savings(guarded, {}, method="engineering_alternative")
        self.assertAlmostEqual(standard.actual_steam_kg, guarded_result.actual_steam_kg)

    def test_inverse_normal_known_quantiles(self):
        self.assertAlmostEqual(0.0, inverse_normal(0.5), places=6)
        self.assertAlmostEqual(1.959964, inverse_normal(0.975), delta=1e-4)
        self.assertAlmostEqual(-1.959964, inverse_normal(0.025), delta=1e-4)


if __name__ == "__main__":
    unittest.main()
