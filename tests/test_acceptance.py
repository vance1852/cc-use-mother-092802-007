import unittest

from beverage_ops_foundation.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])

        retrofit = result["retrofit"]
        self.assertTrue(retrofit["sufficient_data"])
        self.assertEqual(3, retrofit["baseline_count"])
        self.assertEqual(3, retrofit["verification_count"])
        self.assertGreater(retrofit["savings_steam"], 0)
        self.assertGreater(retrofit["savings_amount"], 0)
        # 置信区间整体落在单位蒸汽下降一侧。
        self.assertLess(retrofit["rate_delta_interval"][1], 0)
        self.assertEqual(6, retrofit["effective_measurements"])
        # 已关闭期间在校准失效后保持 closed，只留更正披露。
        self.assertEqual("closed", retrofit["final_status"])
        self.assertGreater(retrofit["disclosed_at_risk"], 0)
        self.assertEqual(64, len(retrofit["snapshot_hash"]))


if __name__ == "__main__":
    unittest.main()
