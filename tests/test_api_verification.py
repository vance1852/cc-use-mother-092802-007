import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.storage import Database

from steam_retrofit_verification.api import route
from steam_retrofit_verification.clock import MutableClock
from steam_retrofit_verification.service import VerificationService


def headers(actor):
    return {"X-Actor-Id": actor}


class VerificationApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
        self.service = VerificationService(self.database, self.clock)
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="酒厂")
        s.register_actor(request_id="act-admin", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="act-eng", actor_id="a1", new_actor_id="eng1",
                         display_name="工程师", role="operator", organization_id="o1")
        s.register_actor(request_id="act-rev", actor_id="a1", new_actor_id="rev1",
                         display_name="复核员", role="reviewer", organization_id="o1")
        s.register_site(request_id="site", actor_id="a1", site_id="s1",
                        organization_id="o1", name="蒸馏车间", timezone_name="Asia/Shanghai")
        self.post = lambda path, body, actor="eng1": route(
            self.service, "POST", path, body, headers(actor))
        self.get = lambda path, actor="eng1": route(
            self.service, "GET", path, None, headers(actor))

    def tearDown(self):
        self.database.close()

    def _seed(self):
        self.post("/boundaries", {"request_id": "bnd", "site_id": "s1",
                                  "boundary_id": "eq1", "name": "蒸汽回用边界"})
        self.post("/meters", {"request_id": "m1", "boundary_id": "eq1", "meter_id": "stm",
                              "name": "蒸汽总表", "metric": "steam", "unit": "kg",
                              "calibration_due": "2027-01-01T00:00:00Z"})
        for i, v in enumerate([1000, 1010, 990, 1005, 995]):
            bid = f"b{i}"
            self.post("/batches", {"request_id": f"pb-{bid}", "boundary_id": "eq1",
                                   "batch_id": bid, "product_code": "A",
                                   "started_at": f"2026-06-{i+1:02d}T06:00:00Z",
                                   "ended_at": f"2026-06-{i+1:02d}T14:00:00Z",
                                   "output": 1000.0})
            self.post("/readings", {"request_id": f"pr-{bid}", "batch_id": bid,
                                    "meter_id": "stm", "steam_kg": v,
                                    "measured_at": f"2026-06-{i+1:02d}T10:00:00Z"})
        for i, v in enumerate([860, 850, 870, 855, 865]):
            bid = f"v{i}"
            self.post("/batches", {"request_id": f"pb-{bid}", "boundary_id": "eq1",
                                   "batch_id": bid, "product_code": "A",
                                   "started_at": f"2026-07-{i+1:02d}T22:00:00Z",
                                   "ended_at": f"2026-07-{i+2:02d}T06:00:00Z",
                                   "output": 1000.0})
            self.post("/readings", {"request_id": f"pr-{bid}", "batch_id": bid,
                                    "meter_id": "stm", "steam_kg": v,
                                    "measured_at": f"2026-07-{i+1:02d}T23:00:00Z"})

    def test_end_to_end_verification_and_explain(self):
        self._seed()
        self.post("/adjustment-factors",
                  {"request_id": "f1", "boundary_id": "eq1", "code": "product_mix",
                   "value": 1.02, "reason": "产品结构折算"}, actor="rev1")
        status, body = self.post("/verifications",
                                 {"request_id": "vc1", "boundary_id": "eq1",
                                  "verification_id": "vr1", "title": "蒸汽技改",
                                  "baseline_start": "2026-06-01T00:00:00Z",
                                  "baseline_end": "2026-06-10T00:00:00Z",
                                  "verification_start": "2026-07-01T00:00:00Z",
                                  "verification_end": "2026-07-10T00:00:00Z"})
        self.assertEqual(201, status)
        status, body = self.post("/snapshots/freeze",
                                 {"request_id": "frz", "verification_id": "vr1"})
        self.assertEqual(201, status)
        status, snap = self.get("/snapshots?verification_id=vr1")
        self.assertEqual(200, status)
        self.assertTrue(snap["payload_hash"])
        self.post("/verifications/submit",
                  {"request_id": "sub", "verification_id": "vr1"})
        status, body = self.post("/verifications/review",
                                 {"request_id": "rvw", "verification_id": "vr1",
                                  "decision": "approve", "comment": "成立"},
                                 actor="rev1")
        self.assertEqual(201, status)

        status, body = self.get("/verifications?verification_id=vr1")
        self.assertEqual(200, status)
        self.assertEqual("confirmed", body["status"])
        self.assertGreater(body["result"]["steam_saved_kg"], 0)
        self.assertGreater(body["result"]["interval_lower"], 0)

        status, explanation = self.get("/verifications/explain?verification_id=vr1")
        self.assertEqual(200, status)
        self.assertEqual(10, len(explanation["effective_measurement_batches"]))
        self.assertIn("stm", explanation["calibration_certificates_used"])
        self.assertEqual([], explanation["excluded"]["missing_batches"])
        evidence = explanation["result"]["evidence"][0]
        self.assertTrue(evidence["included"])
        self.assertEqual(10, len(evidence["batch_ids"]))

    def test_permission_denied_when_operator_reviews(self):
        self._seed()
        self.post("/verifications",
                  {"request_id": "vc1", "boundary_id": "eq1", "verification_id": "vr1",
                   "title": "蒸汽技改",
                   "baseline_start": "2026-06-01T00:00:00Z",
                   "baseline_end": "2026-06-10T00:00:00Z",
                   "verification_start": "2026-07-01T00:00:00Z",
                   "verification_end": "2026-07-10T00:00:00Z"})
        self.post("/snapshots/freeze", {"request_id": "frz", "verification_id": "vr1"})
        self.post("/verifications/submit", {"request_id": "sub", "verification_id": "vr1"})
        status, body = self.post("/verifications/review",
                                 {"request_id": "rvw", "verification_id": "vr1",
                                  "decision": "approve"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", body["error"])

    def test_foundation_routes_still_available(self):
        status, body = self.get("/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])

    def test_missing_verification_id_is_400(self):
        status, body = self.get("/verifications")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"])

    def test_calibration_check_and_suspension_via_api(self):
        self._seed()
        self.post("/verifications",
                  {"request_id": "vc1", "boundary_id": "eq1", "verification_id": "vr1",
                   "title": "蒸汽技改",
                   "baseline_start": "2026-06-01T00:00:00Z",
                   "baseline_end": "2026-06-10T00:00:00Z",
                   "verification_start": "2026-07-01T00:00:00Z",
                   "verification_end": "2026-07-10T00:00:00Z"})
        self.post("/snapshots/freeze", {"request_id": "frz", "verification_id": "vr1"})
        self.post("/verifications/submit", {"request_id": "sub", "verification_id": "vr1"})
        self.post("/verifications/review",
                 {"request_id": "rvw", "verification_id": "vr1", "decision": "approve"},
                 actor="rev1")
        status, body = self.post("/meter-issues",
                                 {"request_id": "iss", "meter_id": "stm",
                                  "issue_from": "2026-07-02T00:00:00Z",
                                  "issue_to": "2026-07-05T00:00:00Z",
                                  "note": "校准漂移"})
        self.assertEqual(201, status)
        self.assertIn("vr1", body["suspended_verifications"])
        status, body = self.get("/verifications?verification_id=vr1")
        self.assertEqual("suspended", body["status"])


if __name__ == "__main__":
    unittest.main()
