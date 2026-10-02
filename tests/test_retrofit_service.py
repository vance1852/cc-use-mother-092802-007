import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import ManualClock
from beverage_ops_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from beverage_ops_foundation.retrofit_service import RetrofitService
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

BASE_WINDOW = ("2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
VER_WINDOW = ("2026-03-01T00:00:00Z", "2026-03-31T23:59:59Z")


class RetrofitServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = ManualClock(datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.retro = RetrofitService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="酒厂")
        self.service.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin",
                                    display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, name, role in [
            ("a-op", "op", "工程", "operator"),
            ("a-rev", "rev", "复核", "reviewer"),
            ("a-aud", "aud", "审计", "auditor"),
        ]:
            self.service.register_actor(request_id=rid, actor_id="admin", new_actor_id=aid,
                                        display_name=name, role=role, organization_id="o1")
        self.service.register_site(request_id="site", actor_id="admin", site_id="s1",
                                   organization_id="o1", name="酿造车间", timezone_name="Asia/Shanghai")
        self.retro.register_boundary(request_id="b1", actor_id="op", boundary_id="bd1",
                                     site_id="s1", name="蒸汽技改边界")
        self.retro.register_meter(request_id="m1", actor_id="op", meter_id="steam-1",
                                  boundary_id="bd1", meter_type="steam", unit="t",
                                  min_range=0, max_range=500)
        self.retro.register_meter(request_id="m2", actor_id="op", meter_id="out-1",
                                  boundary_id="bd1", meter_type="output", unit="hl",
                                  min_range=0, max_range=5000)
        self.retro.register_calibration(request_id="c1", actor_id="op", calibration_id="cal-wide",
                                        meter_id="steam-1", valid_from="2025-12-01T00:00:00Z",
                                        valid_until="2026-12-31T23:59:59Z",
                                        certificate_no="CERT-WIDE-S")
        self.retro.register_calibration(request_id="c2", actor_id="op", calibration_id="cal-out",
                                        meter_id="out-1", valid_from="2025-12-01T00:00:00Z",
                                        valid_until="2026-12-31T23:59:59Z",
                                        certificate_no="CERT-WIDE-O")

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def add_batch(self, bid, kind, day, *, steam=120.0, output=100.0, cost=100.0,
                  product="P1", shift="早班", downtime=0):
        """登记批次及读数；steam=None 表示读数尚未上报（冻结时按缺失处理）。"""

        month = "01" if kind == "baseline" else "03"
        start = f"2026-{month}-{day:02d}T00:00:00Z"
        end = f"2026-{month}-{day:02d}T08:00:00Z"
        self.retro.register_batch(
            request_id=f"batch-{bid}", actor_id="op", batch_id=bid, boundary_id="bd1",
            product_code=product, period_kind=kind, shift_name=shift,
            started_at=start, ended_at=end, output=output, steam_unit_cost=cost,
            downtime_minutes=downtime)
        if steam is not None:
            value = 999.0 if steam == "anom" else steam
            self.retro.record_reading(request_id=f"rs-{bid}", actor_id="op", meter_id="steam-1",
                                      batch_id=bid, observed_at=start, value=value)
        self.retro.record_reading(request_id=f"ro-{bid}", actor_id="op", meter_id="out-1",
                                  batch_id=bid, observed_at=start, value=output)

    def seed_balanced(self):
        """3 个基准批次、3 个验证批次，单位蒸汽约从 1.2 降到 0.9。"""

        self.add_batch("B1", "baseline", 10, steam=120.0, shift="早班")
        self.add_batch("B2", "baseline", 11, steam=124.0, shift="中班", downtime=30)
        self.add_batch("B3", "baseline", 12, steam=118.0, shift="晚班")
        self.add_batch("V1", "verification", 10, steam=98.0, output=105.0, shift="早班")
        self.add_batch("V2", "verification", 11, steam=100.0, output=98.0, shift="中班")
        self.add_batch("V3", "verification", 12, steam=96.0, output=102.0, shift="晚班")

    def freeze(self, request_id="fz", name="季度核验", **kwargs):
        defaults = dict(boundary_id="bd1", name=name,
                        baseline_start=BASE_WINDOW[0], baseline_end=BASE_WINDOW[1],
                        verification_start=VER_WINDOW[0], verification_end=VER_WINDOW[1])
        defaults.update(kwargs)
        receipt = self.retro.freeze_verification(request_id=request_id, actor_id="op", **defaults)
        return receipt.resource_id

    # ------------------------------------------------------------------
    # 登记与权限
    # ------------------------------------------------------------------

    def test_auditor_cannot_register_meter(self):
        with self.assertRaises(PermissionDenied):
            self.retro.register_meter(request_id="x", actor_id="aud", meter_id="steam-9",
                                      boundary_id="bd1", meter_type="steam", unit="t")

    def test_meter_range_validated(self):
        with self.assertRaises(ValidationError):
            self.retro.register_meter(request_id="x", actor_id="op", meter_id="steam-9",
                                      boundary_id="bd1", meter_type="steam", unit="t",
                                      min_range=100, max_range=10)

    def test_calibration_window_validated(self):
        with self.assertRaises(ValidationError):
            self.retro.register_calibration(
                request_id="x", actor_id="op", calibration_id="cal-bad", meter_id="steam-1",
                valid_from="2026-03-01T00:00:00Z", valid_until="2026-02-01T00:00:00Z")

    def test_meter_and_batch_must_share_boundary(self):
        self.retro.register_boundary(request_id="b2", actor_id="op", boundary_id="bd2",
                                     site_id="s1", name="另一条线")
        self.retro.register_batch(
            request_id="batch-bx", actor_id="op", batch_id="BX1", boundary_id="bd2",
            product_code="P1", period_kind="baseline", shift_name="早班",
            started_at="2026-01-10T00:00:00Z", ended_at="2026-01-10T08:00:00Z",
            output=100, steam_unit_cost=100)
        with self.assertRaises(ValidationError):
            self.retro.record_reading(request_id="x", actor_id="op", meter_id="steam-1",
                                      batch_id="BX1", observed_at="2026-01-10T00:00:00Z", value=10)

    # ------------------------------------------------------------------
    # 读数质量
    # ------------------------------------------------------------------

    def test_out_of_range_reading_is_anomalous_and_excludes_batch(self):
        self.add_batch("B1", "baseline", 10, steam=120.0)
        self.add_batch("B2", "baseline", 11, steam="anom")
        self.add_batch("B3", "baseline", 12, steam=121.0)
        self.add_batch("V1", "verification", 10, steam=98.0)
        self.add_batch("V2", "verification", 11, steam=100.0)
        vid = self.freeze()
        explanation = self.retro.explain(vid)
        excluded = {e["batch_id"]: e["reason"] for e in explanation["excluded_batches"]}
        self.assertIn("B2", excluded)
        self.assertIn("超量程异常", excluded["B2"])
        self.assertEqual(1, len(explanation["data_quality"]["anomalous"]))
        self.assertEqual(2, explanation["results"]["baseline_count"])

    def test_missing_reading_excludes_batch(self):
        self.add_batch("B1", "baseline", 10, steam=120.0)
        self.add_batch("B2", "baseline", 11, steam=None)
        self.add_batch("V1", "verification", 10, steam=98.0)
        self.add_batch("V2", "verification", 11, steam=100.0)
        vid = self.freeze()
        explanation = self.retro.explain(vid)
        excluded = {e["batch_id"]: e["reason"] for e in explanation["excluded_batches"]}
        self.assertIn("缺少读数", excluded["B2"])
        self.assertEqual(1, len(explanation["data_quality"]["missing"]))

    def test_no_valid_calibration_at_observation_excludes_batch(self):
        self.add_batch("B1", "baseline", 10, steam=120.0)
        self.add_batch("B2", "baseline", 11, steam=122.0)
        self.add_batch("V1", "verification", 10, steam=98.0)
        self.add_batch("V2", "verification", 11, steam=100.0)
        # 失效蒸汽校准：只覆盖到 1 月 10 日，B2（11 日）失去有效校准。
        self.database.connection.execute(
            "UPDATE retrofit_calibrations SET valid_until=? WHERE calibration_id='cal-wide'",
            ("2026-01-10T23:59:59Z",))
        vid = self.freeze()
        explanation = self.retro.explain(vid)
        excluded = {e["batch_id"]: e["reason"] for e in explanation["excluded_batches"]}
        self.assertIn("无有效校准", excluded["B2"])
        self.assertNotIn("B1", excluded)

    # ------------------------------------------------------------------
    # 快照冻结与不可变性
    # ------------------------------------------------------------------

    def test_freeze_computes_savings_from_effective_measurements(self):
        self.seed_balanced()
        vid = self.freeze()
        explanation = self.retro.explain(vid)
        results = explanation["results"]
        self.assertEqual(3, results["baseline_count"])
        self.assertEqual(3, results["verification_count"])
        self.assertGreater(results["savings_steam"], 0)
        self.assertGreater(results["savings_amount"], 0)
        # 置信区间完全落在“单位蒸汽下降”的一侧。
        self.assertLess(results["rate_delta_high"], 0)
        self.assertGreater(results["margin_of_error"], 0)
        # 每条有效测量都能追溯到读数与校准证书。
        for measurement in explanation["effective_measurements"]:
            self.assertTrue(measurement["sources"])
            for source in measurement["sources"]:
                self.assertTrue(source["calibration_id"])

    def test_snapshot_is_immutable_after_freeze(self):
        self.add_batch("B1", "baseline", 10, steam=120.0)
        self.add_batch("B2", "baseline", 11, steam=None)  # 冻结时缺失
        self.add_batch("V1", "verification", 10, steam=98.0)
        self.add_batch("V2", "verification", 11, steam=100.0)
        vid = self.freeze()
        before = self.retro.get_verification(vid)
        before_hash = before["snapshot_hash"]
        before_results = before["results"]

        # 冻结后读数才补到：相对该快照属于迟到。
        self.clock.advance(hours=2)
        self.retro.record_reading(request_id="rs-B2-late", actor_id="op", meter_id="steam-1",
                                  batch_id="B2", observed_at="2026-01-11T02:00:00Z", value=122.0)
        after = self.retro.get_verification(vid)
        self.assertEqual(before_hash, after["snapshot_hash"])
        self.assertEqual(before_results, after["results"])
        explanation = self.retro.explain(vid)
        late = explanation["data_quality"]["late"]
        self.assertEqual(["B2"], [item["batch_id"] for item in late])
        self.assertEqual(122.0, late[0]["value"])
        # 旧快照中的 B2 仍然是缺失。
        self.assertIn("B2", {e["batch_id"] for e in explanation["excluded_batches"]})

    def test_explicit_missing_reading_can_be_filled_later(self):
        self.add_batch("B1", "baseline", 10, steam=120.0)
        self.add_batch("B2", "baseline", 11, steam=None)
        self.add_batch("V1", "verification", 10, steam=98.0)
        self.add_batch("V2", "verification", 11, steam=100.0)
        # 先把 B2 的蒸汽显式登记为缺失，再补到数值；冻结发生在补到之后，应视为有效。
        self.retro.record_reading(request_id="rs-B2-missing", actor_id="op", meter_id="steam-1",
                                  batch_id="B2", observed_at="2026-01-11T00:00:00Z", value=None)
        self.retro.record_reading(request_id="rs-B2-fill", actor_id="op", meter_id="steam-1",
                                  batch_id="B2", observed_at="2026-01-11T02:00:00Z", value=121.0)
        vid = self.freeze()
        explanation = self.retro.explain(vid)
        self.assertEqual(2, explanation["results"]["baseline_count"])
        self.assertEqual([], explanation["data_quality"]["missing"])

    def test_recorded_reading_cannot_be_overwritten(self):
        self.add_batch("B1", "baseline", 10, steam=120.0)
        with self.assertRaises(ConflictError):
            self.retro.record_reading(request_id="rs-B1-again", actor_id="op", meter_id="steam-1",
                                      batch_id="B1", observed_at="2026-01-10T02:00:00Z", value=130.0)

    def test_refreeze_adopts_previously_late_reading(self):
        self.add_batch("B1", "baseline", 10, steam=120.0)
        self.add_batch("B2", "baseline", 11, steam=None)
        self.add_batch("B3", "baseline", 12, steam=121.0)
        self.add_batch("V1", "verification", 10, steam=98.0)
        self.add_batch("V2", "verification", 11, steam=100.0)
        first = self.freeze(request_id="fz-1")
        self.assertEqual(2, self.retro.explain(first)["results"]["baseline_count"])
        self.clock.advance(hours=2)
        self.retro.record_reading(request_id="rs-B2-late", actor_id="op", meter_id="steam-1",
                                  batch_id="B2", observed_at="2026-01-11T02:00:00Z", value=122.0)
        second = self.freeze(request_id="fz-2", name="补数后复核")
        explanation = self.retro.explain(second)
        self.assertEqual(3, explanation["results"]["baseline_count"])
        self.assertEqual([], explanation["data_quality"]["late"])

    def test_window_boundaries_filter_batches(self):
        self.seed_balanced()
        # 基准窗口只覆盖 1 月 10 日：B2/B3 落在窗口外；验证期跨整个 3 月。
        vid = self.freeze(request_id="fz", baseline_start="2026-01-01T00:00:00Z",
                          baseline_end="2026-01-10T23:59:59Z")
        explanation = self.retro.explain(vid)
        batches = {b["batch_id"] for b in explanation["effective_measurements"]
                   if b["period_kind"] == "baseline"}
        self.assertEqual({"B1"}, batches)

    def test_freeze_uses_injected_clock(self):
        self.seed_balanced()
        vid = self.freeze()
        self.assertEqual("2026-04-01T09:00:00Z", self.retro.get_verification(vid)["frozen_at"])

    def test_cross_midnight_shifts_and_cross_month_windows(self):
        # 基准窗口止于 1 月 31 日：一个跨零点夜班（1-31 22:00 → 2-1 06:00）按 started_at 归入基准期。
        self.add_batch("B1", "baseline", 10, steam=120.0)
        self.retro.register_batch(
            request_id="batch-BX", actor_id="op", batch_id="BX", boundary_id="bd1",
            product_code="P1", period_kind="baseline", shift_name="跨零点夜班",
            started_at="2026-01-31T22:00:00Z", ended_at="2026-02-01T06:00:00Z",
            output=100.0, steam_unit_cost=100.0)
        self.retro.record_reading(request_id="rs-BX", actor_id="op", meter_id="steam-1",
                                  batch_id="BX", observed_at="2026-01-31T22:00:00Z", value=121.0)
        self.retro.record_reading(request_id="ro-BX", actor_id="op", meter_id="out-1",
                                  batch_id="BX", observed_at="2026-01-31T22:00:00Z", value=100.0)
        # 验证窗口止于 3 月 31 日：跨月夜班（3-31 22:00 → 4-1 06:00）归入验证期。
        self.add_batch("V1", "verification", 10, steam=98.0)
        self.retro.register_batch(
            request_id="batch-VX", actor_id="op", batch_id="VX", boundary_id="bd1",
            product_code="P1", period_kind="verification", shift_name="跨月夜班",
            started_at="2026-03-31T22:00:00Z", ended_at="2026-04-01T06:00:00Z",
            output=100.0, steam_unit_cost=100.0)
        self.retro.record_reading(request_id="rs-VX", actor_id="op", meter_id="steam-1",
                                  batch_id="VX", observed_at="2026-03-31T22:00:00Z", value=97.0)
        self.retro.record_reading(request_id="ro-VX", actor_id="op", meter_id="out-1",
                                  batch_id="VX", observed_at="2026-03-31T22:00:00Z", value=100.0)

        before = self.clock.now()
        vid = self.freeze(request_id="fz-cross")
        explanation = self.retro.explain(vid)
        by_batch = {m["batch_id"]: m for m in explanation["effective_measurements"]}
        self.assertEqual({"B1", "BX"}, {b for b, m in by_batch.items() if m["period_kind"] == "baseline"})
        self.assertEqual({"V1", "VX"}, {b for b, m in by_batch.items() if m["period_kind"] == "verification"})
        # 冻结时间来自注入时钟；推进时钟不改变已冻结快照。
        self.assertEqual(before.isoformat().replace("+00:00", "Z"),
                         self.retro.get_verification(vid)["frozen_at"])
        self.clock.advance(days=40)
        self.assertEqual(2, explanation["results"]["verification_count"])

    def test_freeze_request_is_idempotent(self):
        self.seed_balanced()
        first = self.freeze(request_id="same")
        second = self.freeze(request_id="same")
        self.assertEqual(first, second)

    # ------------------------------------------------------------------
    # 调整因子
    # ------------------------------------------------------------------

    def test_unapproved_factor_is_not_applied(self):
        self.seed_balanced()
        self.retro.propose_adjustment_factor(
            request_id="f1", actor_id="op", factor_id="fac-1", boundary_id="bd1",
            period_kind="baseline", factor=0.9, reason="结构归一化")
        vid = self.freeze()
        batches = {b["batch_id"]: b for b in self.retro.explain(vid)["effective_measurements"]
                   if b["period_kind"] == "baseline"}
        self.assertTrue(all(b["factor_id"] is None for b in batches.values()))
        self.assertAlmostEqual(1.0, batches["B1"]["adjustment_factor"])

    def test_approved_factor_is_applied_after_independent_review(self):
        self.seed_balanced()
        self.retro.propose_adjustment_factor(
            request_id="f1", actor_id="op", factor_id="fac-1", boundary_id="bd1",
            period_kind="baseline", factor=0.9, reason="结构归一化")
        with self.assertRaises(PermissionDenied):
            self.retro.review_adjustment_factor(request_id="f-op", actor_id="op",
                                                factor_id="fac-1", decision="approved")
        self.retro.review_adjustment_factor(request_id="f-rev", actor_id="rev", factor_id="fac-1",
                                            decision="approved", note="核对通过")
        vid = self.freeze()
        b1 = next(b for b in self.retro.explain(vid)["effective_measurements"] if b["batch_id"] == "B1")
        self.assertEqual("fac-1", b1["factor_id"])
        self.assertAlmostEqual(0.9, b1["adjustment_factor"])
        self.assertAlmostEqual(1.08, b1["adjusted_rate"])

    # ------------------------------------------------------------------
    # 独立复核确认
    # ------------------------------------------------------------------

    def test_only_reviewer_confirms_final_savings(self):
        self.seed_balanced()
        vid = self.freeze()
        with self.assertRaises(PermissionDenied):
            self.retro.confirm_verification(request_id="cf-op", actor_id="op", verification_id=vid)
        self.retro.confirm_verification(request_id="cf-rev", actor_id="rev",
                                        verification_id=vid, note="通过")
        self.assertEqual("confirmed", self.retro.get_verification(vid)["status"])
        with self.assertRaises(ConflictError):
            self.retro.confirm_verification(request_id="cf-again", actor_id="rev", verification_id=vid)

    def test_insufficient_data_blocks_confirmation(self):
        self.add_batch("B1", "baseline", 10, steam=120.0)
        self.add_batch("V1", "verification", 10, steam=98.0)
        self.add_batch("V2", "verification", 11, steam=100.0)
        vid = self.freeze()
        self.assertFalse(self.retro.get_verification(vid)["sufficient_data"])
        with self.assertRaises(ConflictError):
            self.retro.confirm_verification(request_id="cf", actor_id="rev", verification_id=vid)

    def test_only_confirmed_verification_can_close(self):
        self.seed_balanced()
        vid = self.freeze()
        with self.assertRaises(ConflictError):
            self.retro.close_verification(request_id="cl-x", actor_id="rev", verification_id=vid)
        self.retro.confirm_verification(request_id="cf", actor_id="rev", verification_id=vid)
        self.retro.close_verification(request_id="cl", actor_id="rev", verification_id=vid)
        self.assertEqual("closed", self.retro.get_verification(vid)["status"])

    # ------------------------------------------------------------------
    # 替代计算
    # ------------------------------------------------------------------

    def test_alternate_calculation_creates_reviewed_child(self):
        self.seed_balanced()
        parent = self.freeze(name="原始口径")
        with self.assertRaises(PermissionDenied):
            self.retro.review_alternate(request_id="alt-op", actor_id="op",
                                        alternate_id="alt-1", decision="accepted")
        alt = self.retro.propose_alternate(
            request_id="alt-1", actor_id="op", verification_id=parent,
            excluded_batches=["B2"], rationale="计划外停机 30 分钟")
        review = self.retro.review_alternate(
            request_id="alt-rev", actor_id="rev", alternate_id=alt.resource_id,
            decision="accepted", note="停机记录属实")
        receipt = json_load_receipt(self.database, "alt-rev")
        child = receipt["child_verification_id"]
        self.assertTrue(child)
        self.assertEqual("superseded", self.retro.get_verification(parent)["status"])
        child_view = self.retro.get_verification(child)
        self.assertEqual(parent, child_view["parent_verification_id"])
        effective = child_view["results"]["effective_batch_ids"]
        self.assertNotIn("B2", effective)
        self.assertIn("B1", effective)
        # 子核验是独立快照，需要单独确认。
        self.assertEqual("frozen", child_view["status"])
        self.retro.confirm_verification(request_id="cf-child", actor_id="rev", verification_id=child)

    def test_rejected_alternate_keeps_parent_frozen(self):
        self.seed_balanced()
        parent = self.freeze()
        alt = self.retro.propose_alternate(
            request_id="alt-1", actor_id="op", verification_id=parent,
            excluded_batches=["B2"], rationale="停机")
        self.retro.review_alternate(request_id="alt-rev", actor_id="rev",
                                   alternate_id=alt.resource_id, decision="rejected", note="证据不足")
        self.assertEqual("frozen", self.retro.get_verification(parent)["status"])

    def test_alternate_must_reference_snapshot_batches(self):
        self.seed_balanced()
        parent = self.freeze()
        with self.assertRaises(ValidationError):
            self.retro.propose_alternate(
                request_id="alt-1", actor_id="op", verification_id=parent,
                excluded_batches=["NOPE"], rationale="不存在的批次")

    # ------------------------------------------------------------------
    # 校准失效：暂停与更正披露
    # ------------------------------------------------------------------

    def _three_settlements(self):
        self.seed_balanced()
        closed = self.freeze(request_id="fz-1", name="已关闭期间")
        self.retro.confirm_verification(request_id="cf-1", actor_id="rev", verification_id=closed)
        self.retro.close_verification(request_id="cl-1", actor_id="rev", verification_id=closed)
        confirmed = self.freeze(request_id="fz-2", name="已确认未结算")
        self.retro.confirm_verification(request_id="cf-2", actor_id="rev", verification_id=confirmed)
        frozen = self.freeze(request_id="fz-3", name="刚冻结")
        return closed, confirmed, frozen

    def test_calibration_failure_holds_unsettled_and_discloses_closed(self):
        closed, confirmed, frozen = self._three_settlements()
        receipt = self.retro.report_calibration_failure(
            request_id="corr-1", actor_id="aud", meter_id="steam-1",
            effective_from="2026-03-01T00:00:00Z", reason="证书核查未通过",
            calibration_id="cal-wide")
        impacts = {item["verification_id"]: item for item in json_load_receipt(self.database, "corr-1")["impacts"]}
        # 已关闭：只披露，状态不变。
        self.assertEqual("disclosure", impacts[closed]["action"])
        self.assertEqual("closed", self.retro.get_verification(closed)["status"])
        # 已确认、刚冻结：暂停未结算收益。
        self.assertEqual("hold", impacts[confirmed]["action"])
        self.assertEqual("hold", impacts[frozen]["action"])
        self.assertEqual("calibration_hold", self.retro.get_verification(confirmed)["status"])
        self.assertEqual("calibration_hold", self.retro.get_verification(frozen)["status"])
        # 失效日在 3 月 1 日：每份核验只命中 3 条验证期蒸汽读数，基准期读数不受影响。
        for impact in impacts.values():
            self.assertEqual(3, impact["affected_readings"])
        # 暂停期间禁止确认。
        with self.assertRaises(ConflictError):
            self.retro.confirm_verification(request_id="cf-x", actor_id="rev", verification_id=confirmed)
        # 披露记录可经 explain 查看，金额为该快照原结论的风险金额。
        disclosed = self.retro.explain(closed)["calibration_impacts"]
        self.assertEqual("disclosure", disclosed[0]["action"])
        self.assertGreater(disclosed[0]["savings_amount_at_risk"], 0)

    def test_calibration_failure_before_window_does_not_hit_baseline(self):
        closed, confirmed, frozen = self._three_settlements()
        self.retro.report_calibration_failure(
            request_id="corr-1", actor_id="rev", meter_id="steam-1",
            effective_from="2026-01-01T00:00:00Z", reason="全周期失效")
        impacts = json_load_receipt(self.database, "corr-1")["impacts"]
        # 每个快照 3 基准 + 3 验证共 6 条蒸汽读数。
        self.assertTrue(all(item["affected_readings"] == 6 for item in impacts))

    def test_failure_without_affected_readings_records_no_impact(self):
        self.seed_balanced()
        vid = self.freeze()
        self.retro.report_calibration_failure(
            request_id="corr-1", actor_id="aud", meter_id="steam-1",
            effective_from="2026-09-01T00:00:00Z", reason="未来事件")
        self.assertEqual([], json_load_receipt(self.database, "corr-1")["impacts"])
        self.assertEqual("frozen", self.retro.get_verification(vid)["status"])

    def test_correction_can_be_cleared_without_changing_holds(self):
        _, confirmed, _ = self._three_settlements()
        reported = self.retro.report_calibration_failure(
            request_id="corr-1", actor_id="aud", meter_id="steam-1",
            effective_from="2026-03-01T00:00:00Z", reason="证书失效")
        self.retro.clear_correction(request_id="cc", actor_id="rev",
                                    correction_id=reported.resource_id)
        # 更正关闭不等于自动恢复结算：核验仍暂停，需重新冻结。
        self.assertEqual("calibration_hold", self.retro.get_verification(confirmed)["status"])

    # ------------------------------------------------------------------
    # 审计链
    # ------------------------------------------------------------------

    def test_audit_chain_covers_retrofit_lifecycle(self):
        self.seed_balanced()
        self.retro.propose_adjustment_factor(
            request_id="f1", actor_id="op", factor_id="fac-1", boundary_id="bd1",
            period_kind="baseline", factor=0.97, reason="结构归一化")
        vid = self.freeze()
        self.retro.confirm_verification(request_id="cf", actor_id="rev", verification_id=vid)
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 10)
        actions = {event["action"] for event in self.service.audit_events()}
        self.assertIn("retrofit.verification.frozen", actions)
        self.assertIn("retrofit.verification.confirmed", actions)
        self.assertIn("retrofit.factor.proposed", actions)


def json_load_receipt(database, request_id):
    import json

    row = database.connection.execute(
        "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,)
    ).fetchone()
    return json.loads(row[0])


if __name__ == "__main__":
    unittest.main()
