import unittest

from steam_retrofit_verification.acceptance import run


class RetrofitAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual("closed", result["verification_status"])
        self.assertTrue(result["suspended_on_issue"])
        self.assertEqual(1, result["late_arrivals_flagged"])
        self.assertFalse(result["late_reading_changed_snapshot"])
        self.assertEqual(1, result["closed_period_corrections"])
        self.assertTrue(result["closed_period_status_unchanged"])
        self.assertEqual(10, result["effective_batches"])
        self.assertGreater(result["steam_saved_kg"], 0)
        self.assertLess(result["confidence_interval"][0], result["confidence_interval"][1])


if __name__ == "__main__":
    unittest.main()
