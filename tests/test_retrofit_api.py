import json
import unittest

from beverage_ops_foundation.api import route
from beverage_ops_foundation.retrofit_service import RetrofitService
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database


class RetrofitApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.retro = RetrofitService(self.database)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="酒厂")
        self.service.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="a-op", actor_id="admin", new_actor_id="op",
                                    display_name="工程", role="operator", organization_id="o1")
        self.service.register_actor(request_id="a-rev", actor_id="admin", new_actor_id="rev",
                                    display_name="复核", role="reviewer", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="admin", site_id="s1",
                                   organization_id="o1", name="车间", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op"):
        return route(self.service, method, path, body, {"X-Actor-Id": actor})

    def seed(self):
        self.call("POST", "/retrofit/boundaries",
                  {"request_id": "b1", "boundary_id": "bd1", "site_id": "s1", "name": "蒸汽边界"})
        self.call("POST", "/retrofit/meters",
                  {"request_id": "m1", "meter_id": "steam-1", "boundary_id": "bd1",
                   "meter_type": "steam", "unit": "t", "min_range": 0, "max_range": 500})
        self.call("POST", "/retrofit/meters",
                  {"request_id": "m2", "meter_id": "out-1", "boundary_id": "bd1",
                   "meter_type": "output", "unit": "hl", "min_range": 0, "max_range": 5000})
        self.call("POST", "/retrofit/calibrations",
                  {"request_id": "c1", "calibration_id": "cal-s", "meter_id": "steam-1",
                   "valid_from": "2025-12-01T00:00:00Z",
                   "valid_until": "2026-12-31T23:59:59Z"})
        self.call("POST", "/retrofit/calibrations",
                  {"request_id": "c2", "calibration_id": "cal-o", "meter_id": "out-1",
                   "valid_from": "2025-12-01T00:00:00Z",
                   "valid_until": "2026-12-31T23:59:59Z"})
        for bid, kind, day, steam, out in [
            ("B1", "baseline", 10, 120.0, 100.0), ("B2", "baseline", 11, 124.0, 100.0),
            ("V1", "verification", 10, 98.0, 100.0), ("V2", "verification", 11, 100.0, 100.0)]:
            month = "01" if kind == "baseline" else "03"
            self.call("POST", "/retrofit/batches",
                      {"request_id": f"batch-{bid}", "batch_id": bid, "boundary_id": "bd1",
                       "product_code": "P1", "period_kind": kind, "shift_name": "早班",
                       "started_at": f"2026-{month}-{day:02d}T00:00:00Z",
                       "ended_at": f"2026-{month}-{day:02d}T08:00:00Z",
                       "output": out, "steam_unit_cost": 100})
            self.call("POST", "/retrofit/readings",
                      {"request_id": f"rs-{bid}", "meter_id": "steam-1", "batch_id": bid,
                       "observed_at": f"2026-{month}-{day:02d}T00:00:00Z", "value": steam})
            self.call("POST", "/retrofit/readings",
                      {"request_id": f"ro-{bid}", "meter_id": "out-1", "batch_id": bid,
                       "observed_at": f"2026-{month}-{day:02d}T00:00:00Z", "value": out})

    def test_full_review_workflow_over_http(self):
        self.seed()
        status, payload = self.call("POST", "/retrofit/verifications", {
            "request_id": "fz", "boundary_id": "bd1", "name": "Q1核验",
            "baseline_start": "2026-01-01T00:00:00Z", "baseline_end": "2026-01-31T23:59:59Z",
            "verification_start": "2026-03-01T00:00:00Z",
            "verification_end": "2026-03-31T23:59:59Z"})
        self.assertEqual(201, status)
        vid = payload["resource_id"]

        status, payload = self.call("GET", f"/retrofit/verifications/{vid}/explain", actor="rev")
        self.assertEqual(200, status)
        self.assertGreater(payload["results"]["savings_amount"], 0)
        self.assertEqual(4, len(payload["effective_measurements"]))
        self.assertIsNotNone(payload["confidence"])

        # 工程角色确认被拒。
        status, payload = self.call("POST", "/retrofit/verifications/confirm",
                                    {"request_id": "cf-op", "verification_id": vid}, actor="op")
        self.assertEqual(403, status)
        status, _ = self.call("POST", "/retrofit/verifications/confirm",
                              {"request_id": "cf", "verification_id": vid, "note": "通过"}, actor="rev")
        self.assertEqual(201, status)

        status, payload = self.call("GET", "/retrofit/verifications?boundary_id=bd1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("confirmed", payload["items"][0]["status"])

    def test_calibration_failure_hold_over_http(self):
        self.seed()
        status, payload = self.call("POST", "/retrofit/verifications", {
            "request_id": "fz", "boundary_id": "bd1", "name": "Q1核验",
            "baseline_start": "2026-01-01T00:00:00Z", "baseline_end": "2026-01-31T23:59:59Z",
            "verification_start": "2026-03-01T00:00:00Z",
            "verification_end": "2026-03-31T23:59:59Z"})
        vid = payload["resource_id"]
        status, payload = self.call("POST", "/retrofit/calibration-failures", {
            "request_id": "corr", "meter_id": "steam-1",
            "effective_from": "2026-03-01T00:00:00Z", "reason": "证书核查未通过",
            "calibration_id": "cal-s"}, actor="rev")
        self.assertEqual(201, status)
        self.assertEqual("hold", payload["impacts"][0]["action"])
        status, payload = self.call("GET", f"/retrofit/verifications/{vid}")
        self.assertEqual("calibration_hold", payload["status"])
        self.assertIn("校准失效", payload["hold_reason"])

    def test_unknown_retrofit_route_is_404(self):
        status, payload = self.call("GET", "/retrofit/nope")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_verification_listing_requires_boundary(self):
        status, payload = self.call("GET", "/retrofit/verifications")
        self.assertEqual(400, status)


if __name__ == "__main__":
    unittest.main()
