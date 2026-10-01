import unittest
from datetime import datetime, timedelta, timezone

from beverage_ops_foundation.errors import ConflictError, PermissionDenied, ValidationError
from beverage_ops_foundation.storage import Database

from steam_retrofit_verification.clock import MutableClock
from steam_retrofit_verification.service import VerificationService


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
        self.service = VerificationService(self.database, self.clock)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="酒厂")
        s.register_actor(request_id="act-admin", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="act-eng", actor_id="a1", new_actor_id="eng1",
                         display_name="工程师", role="operator", organization_id="o1")
        s.register_actor(request_id="act-rev", actor_id="a1", new_actor_id="rev1",
                         display_name="复核员", role="reviewer", organization_id="o1")
        s.register_actor(request_id="act-eng2", actor_id="a1", new_actor_id="eng2",
                         display_name="工程师二", role="operator", organization_id="o1")
        s.register_site(request_id="site", actor_id="a1", site_id="s1",
                        organization_id="o1", name="蒸馏车间", timezone_name="Asia/Shanghai")
        s.register_boundary(request_id="bnd", actor_id="eng1", site_id="s1",
                            boundary_id="eq1", name="蒸汽冷凝水回用边界")
        s.register_meter_point(request_id="m1", actor_id="eng1", boundary_id="eq1",
                               meter_id="stm", name="蒸汽总表", metric="steam",
                               unit="kg", calibration_due="2027-01-01T00:00:00Z")

    def _batches_and_readings(self, base_vals, ver_vals):
        s = self.service
        # 基准期：2026-06 上半月（跨班次），验证期：2026-07 上半月（跨月）。
        for i, v in enumerate(base_vals):
            day = 1 + i
            bid = f"base{i}"
            s.register_batch(request_id=f"pb-{bid}", actor_id="eng1", boundary_id="eq1",
                             batch_id=bid, product_code="LiquorA",
                             started_at=f"2026-06-{day:02d}T06:00:00Z",
                             ended_at=f"2026-06-{day:02d}T14:00:00Z", output=1000.0)
            if v is not None:
                s.record_reading(request_id=f"pr-{bid}", actor_id="eng1", batch_id=bid,
                                 meter_id="stm", steam_kg=v,
                                 measured_at=f"2026-06-{day:02d}T10:00:00Z")
        for i, v in enumerate(ver_vals):
            day = 1 + i
            bid = f"ver{i}"
            s.register_batch(request_id=f"pb-{bid}", actor_id="eng1", boundary_id="eq1",
                             batch_id=bid, product_code="LiquorA",
                             started_at=f"2026-07-{day:02d}T22:00:00Z",
                             ended_at=f"2026-07-{day + 1:02d}T06:00:00Z", output=1000.0)
            if v is not None:
                s.record_reading(request_id=f"pr-{bid}", actor_id="eng1", batch_id=bid,
                                 meter_id="stm", steam_kg=v,
                                 measured_at=f"2026-07-{day:02d}T23:00:00Z")

    def _create_verification(self, vid="v1"):
        self.service.create_verification(
            request_id=f"vc-{vid}", actor_id="eng1", boundary_id="eq1", verification_id=vid,
            title="蒸汽回用技改收益核验",
            baseline_start="2026-06-01T00:00:00Z", baseline_end="2026-06-10T00:00:00Z",
            verification_start="2026-07-01T00:00:00Z", verification_end="2026-07-10T00:00:00Z",
            confidence_level=0.95)

    def _approve_factor(self):
        self.service.register_adjustment_factor(
            request_id="f1", actor_id="rev1", boundary_id="eq1",
            code="product_mix", value=1.02, reason="验证期高度酒占比上升，折算+2%")

    # ------------------------------------------------------------- 主流程

    def test_full_happy_path_with_snapshot_and_independent_review(self):
        s = self.service
        self._batches_and_readings([1000, 1010, 990, 1005, 995],
                                   [860, 850, 870, 855, 865])
        self._approve_factor()
        self._create_verification()
        receipt = s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="v1")
        self.assertFalse(receipt.replayed)
        snapshot = s.get_snapshot("v1")
        self.assertEqual(10, len(snapshot.payload["batches"]))
        self.assertTrue(snapshot.payload_hash)

        v = s.get_verification("v1")
        self.assertGreater(v.result.steam_saved_kg, 0)
        self.assertGreater(v.result.interval_lower, 0)  # 节能显著，区间下界仍为正
        self.assertIn("product_mix", v.result.adjustments)

        s.submit_for_review(request_id="sub", actor_id="eng1", verification_id="v1")
        # 提单人不能自审
        with self.assertRaises(PermissionDenied):
            s.review_verification(request_id="self", actor_id="eng1", verification_id="v1",
                                  decision="approve")
        s.review_verification(request_id="rev", actor_id="rev1", verification_id="v1",
                              decision="approve", comment="测量链完整，节能成立")
        v = s.get_verification("v1")
        self.assertEqual("confirmed", v.status)
        self.assertEqual("rev1", v.confirmed_by)

    def test_operator_role_cannot_review(self):
        s = self.service
        self._batches_and_readings([1000, 1010, 990, 1005, 995],
                                   [860, 850, 870, 855, 865])
        self._create_verification("vx")
        s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="vx")
        s.submit_for_review(request_id="sub", actor_id="eng1", verification_id="vx")
        # eng2 是 operator，不具备复核角色。
        with self.assertRaises(PermissionDenied):
            s.review_verification(request_id="r2", actor_id="eng2", verification_id="vx",
                                  decision="approve")

    def test_proposer_cannot_be_independent_reviewer_even_as_admin(self):
        # admin 同时具备工程与复核角色，但提单人不能做自己的独立复核者。
        s = self.service
        self._batches_and_readings([1000, 1010, 990, 1005, 995],
                                   [860, 850, 870, 855, 865])
        s.create_verification(
            request_id="vc-admin", actor_id="a1", boundary_id="eq1", verification_id="va",
            title="管理员提单",
            baseline_start="2026-06-01T00:00:00Z", baseline_end="2026-06-10T00:00:00Z",
            verification_start="2026-07-01T00:00:00Z", verification_end="2026-07-10T00:00:00Z")
        s.freeze_snapshot(request_id="frz-a", actor_id="a1", verification_id="va")
        s.submit_for_review(request_id="sub-a", actor_id="a1", verification_id="va")
        with self.assertRaises(PermissionDenied) as ctx:
            s.review_verification(request_id="self-a", actor_id="a1", verification_id="va",
                                  decision="approve")
        self.assertIn("独立复核", str(ctx.exception))
        s.review_verification(request_id="rev-a", actor_id="rev1", verification_id="va",
                              decision="approve")
        self.assertEqual("confirmed", s.get_verification("va").status)

    def test_engineering_alternative_flow_rejected_then_resubmitted(self):
        s = self.service
        # 验证期只有 ver0 有真实读数，其余 4 个批次读数缺失，标准口径不足无法冻结。
        self._batches_and_readings([1000, 1010, 990, 1005, 995],
                                   [860, None, None, None, None])
        self._create_verification()
        with self.assertRaises(ValidationError):
            s.freeze_snapshot(request_id="frz-bad", actor_id="eng1", verification_id="v1")
        s.propose_alternative(request_id="alt", actor_id="eng1", verification_id="v1",
                              rationale="4 个批次仪表通信中断，按相邻班次均值 860 估算",
                              overrides={"ver1": 860.0, "ver2": 858.0,
                                         "ver3": 862.0, "ver4": 859.0})
        s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="v1")
        v = s.get_verification("v1")
        self.assertEqual("engineering_alternative", v.result.method)
        self.assertEqual(5, v.result.verification_n)
        self.assertEqual(4, v.result.missing_count)
        s.submit_for_review(request_id="sub", actor_id="eng1", verification_id="v1")
        s.review_verification(request_id="rv", actor_id="rev1", verification_id="v1",
                              decision="reject", comment="替代依据不足")
        self.assertEqual("rejected", s.get_verification("v1").status)
        # 驳回后补充停机台账佐证，更新替代值并重新冻结、提交、确认
        s.propose_alternative(request_id="alt2", actor_id="eng1", verification_id="v1",
                              rationale="补充停机台账佐证，按相邻班次均值微调",
                              overrides={"ver1": 861.0, "ver2": 859.0,
                                         "ver3": 863.0, "ver4": 860.0})
        s.freeze_snapshot(request_id="frz2", actor_id="eng1", verification_id="v1")
        s.submit_for_review(request_id="sub2", actor_id="eng1", verification_id="v1")
        s.review_verification(request_id="rv2", actor_id="rev1", verification_id="v1",
                              decision="approve", comment="佐证可接受")
        self.assertEqual("confirmed", s.get_verification("v1").status)

    def test_late_reading_excluded_from_frozen_snapshot_but_flagged(self):
        s = self.service
        self._batches_and_readings([1000, 1010, 990, 1005, 995],
                                   [860, 850, 870, 855, 865])
        self._create_verification()
        s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="v1")
        saved_before = s.get_verification("v1").result.steam_saved_kg

        # 冻结之后，基准批次的迟到修正读数到达
        self.clock.advance(timedelta(days=3))
        s.record_reading(request_id="late-1", actor_id="eng1", batch_id="base0",
                         meter_id="stm", steam_kg=700.0,
                         measured_at="2026-06-01T10:00:00Z")

        quality = s.reading_quality("v1")
        self.assertEqual(1, len(quality["late_arrivals"]))
        self.assertEqual("base0", quality["late_arrivals"][0]["batch_id"])
        # 冻结结果不被迟到读数改变
        snapshot = s.get_snapshot("v1")
        self.assertNotIn(700.0, [r["steam_kg"] for r in snapshot.payload["readings"]
                                 if r["batch_id"] == "base0"])
        self.assertAlmostEqual(saved_before,
                               s.replay_from_snapshot("v1").steam_saved_kg)

    def test_calibration_failure_suspends_unsettled_benefit(self):
        s = self.service
        self._batches_and_readings([1000, 1010, 990, 1005, 995],
                                   [860, 850, 870, 855, 865])
        self._create_verification()
        s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="v1")
        s.submit_for_review(request_id="sub", actor_id="eng1", verification_id="v1")
        s.review_verification(request_id="rv", actor_id="rev1", verification_id="v1",
                              decision="approve")
        # 报告蒸汽总表在验证期失效
        impact = s.report_meter_issue(
            request_id="iss", actor_id="eng1", meter_id="stm",
            issue_from="2026-07-02T00:00:00Z", issue_to="2026-07-05T00:00:00Z",
            note="发现校准漂移")
        self.assertIn("v1", impact["suspended_verifications"])
        self.assertEqual("suspended", s.get_verification("v1").status)
        # 暂停期间不得结算
        with self.assertRaises(ConflictError):
            s.settle_benefit(request_id="set", actor_id="a1", verification_id="v1")
        # 失效排除后复核恢复
        s.resolve_meter_issue(request_id="ris", actor_id="rev1", issue_id=impact["receipt"]["resource_id"])
        s.resume_verification(request_id="res", actor_id="rev1", verification_id="v1",
                              comment="重新校准合格")
        self.assertEqual("confirmed", s.get_verification("v1").status)

    def test_closed_period_only_discloses_via_correction(self):
        s = self.service
        self._batches_and_readings([1000, 1010, 990, 1005, 995],
                                   [860, 850, 870, 855, 865])
        self._create_verification()
        s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="v1")
        s.submit_for_review(request_id="sub", actor_id="eng1", verification_id="v1")
        s.review_verification(request_id="rv", actor_id="rev1", verification_id="v1",
                              decision="approve")
        s.settle_benefit(request_id="set", actor_id="a1", verification_id="v1")
        s.close_period(request_id="cls", actor_id="a1", verification_id="v1")
        self.assertEqual("closed", s.get_verification("v1").status)
        saved = s.get_verification("v1").result.steam_saved_kg

        # 关闭后报告计量失效：不能暂停，只能走更正披露
        impact = s.report_meter_issue(
            request_id="iss", actor_id="rev1", meter_id="stm",
            issue_from="2026-07-01T00:00:00Z", issue_to="2026-07-08T00:00:00Z",
            note="关闭后审计发现总表漂移")
        self.assertIn("v1", impact["verifications_requiring_correction"])
        self.assertEqual("closed", s.get_verification("v1").status)
        # 冻结收益数字不变
        self.assertEqual(saved, s.get_verification("v1").result.steam_saved_kg)

        s.record_correction(
            request_id="cor", actor_id="rev1", verification_id="v1",
            issue_id=impact["receipt"]["resource_id"],
            reason="历史校准漂移使验证期蒸汽被低估",
            disclosed_impact={"estimated_overstatement_kg": 320.0,
                              "restated_unit_cost_change": 0.11})
        corrections = s.list_corrections("v1")
        self.assertEqual(1, len(corrections))
        self.assertEqual(["stm"], corrections[0].impacted_meter_ids)
        # 已关闭核验不能再恢复或重新冻结
        with self.assertRaises(ConflictError):
            s.resume_verification(request_id="res2", actor_id="rev1", verification_id="v1")

    def test_anomalous_and_missing_readings_are_classified(self):
        s = self.service
        # ver0 异常读数（负数会被接口拒绝，这里用校准失效制造异常），ver4 缺失
        self._batches_and_readings([1000, 1010, 990, 1005, 995],
                                   [860, 850, 870, 855, None])
        # 登记一张短期校准证书，在验证期测量时刻已失效
        s.record_calibration(request_id="cal-short", actor_id="eng1", meter_id="stm",
                             certified_at="2026-05-01T00:00:00Z",
                             valid_until="2026-06-20T00:00:00Z",
                             certificate_ref="SHORT")
        self._create_verification()
        with self.assertRaises(ValidationError):
            s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="v1")
        # 标准口径全部 ver 批次异常/缺失；工程替代口径把 5 个批次全部救回
        s.propose_alternative(request_id="alt", actor_id="eng1", verification_id="v1",
                              rationale="校准失效与缺失批次以相邻班次估算",
                              overrides={"ver0": 860.0, "ver1": 850.0, "ver2": 870.0,
                                         "ver3": 855.0, "ver4": 865.0})
        receipt = s.freeze_snapshot(request_id="frz2", actor_id="eng1", verification_id="v1")
        self.assertFalse(receipt.replayed)
        # 标准口径不足的警告进入审计链
        events = s.audit_events()
        freeze_events = [e for e in events if e["action"] == "snapshot.frozen"]
        self.assertEqual("standard_insufficient_measurements",
                         freeze_events[-1]["detail"]["standard_warning"])
        result = s.get_verification("v1").result
        self.assertEqual(5, result.verification_n)
        self.assertEqual(5, result.anomalous_count + result.missing_count)

    def test_explain_attributes_numbers_to_measurements(self):
        s = self.service
        self._batches_and_readings([1000, 1010, 990, 1005, 995],
                                   [860, 850, 870, 855, 865])
        self._approve_factor()
        self._create_verification()
        s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="v1")
        explanation = s.explain("v1")
        self.assertEqual(10, len(explanation["effective_measurement_batches"]))
        self.assertIn("stm", explanation["calibration_certificates_used"])
        self.assertEqual({"product_mix": 1.02},
                         explanation["approved_adjustment_factors"])
        meter_evidence = [e for e in explanation["result"]["evidence"]
                          if e["meter_id"] == "stm"][0]
        self.assertTrue(meter_evidence["included"])

    def test_calibration_expiry_detected_by_clock_suspends_benefit(self):
        s = self.service
        # 计量点校准在 2026-07-05 失效，验证期读数晚于该日
        s.register_meter_point(request_id="m2", actor_id="eng1", boundary_id="eq1",
                               meter_id="stm2", name="蒸汽分表", metric="steam",
                               unit="kg", calibration_due="2026-07-05T00:00:00Z")
        for i, v in enumerate([860, 850, 870, 855, 865]):
            day = 6 + i
            bid = f"ver{i}"
            s.register_batch(request_id=f"pb-{bid}", actor_id="eng1", boundary_id="eq1",
                             batch_id=bid, product_code="A",
                             started_at=f"2026-07-{day:02d}T06:00:00Z",
                             ended_at=f"2026-07-{day:02d}T14:00:00Z", output=1000.0)
            # 校准声明证书覆盖到 2026-07-05，测量时刻已过期 → 读数本应异常，
            # 这里先登记覆盖测量时刻的证书，确认收益后再让证书被新证书缩短。
        # 用覆盖全部测量时刻的证书登记
        s.record_calibration(request_id="cal-ok", actor_id="eng1", meter_id="stm2",
                             certified_at="2026-01-01T00:00:00Z",
                             valid_until="2026-12-31T00:00:00Z",
                             certificate_ref="GOOD-CERT")
        for i, v in enumerate([860, 850, 870, 855, 865]):
            day = 6 + i
            s.record_reading(request_id=f"pr-ver{i}", actor_id="eng1", batch_id=f"ver{i}",
                             meter_id="stm2", steam_kg=v,
                             measured_at=f"2026-07-{day:02d}T10:00:00Z")
        for i, v in enumerate([1000, 1010, 990, 1005, 995]):
            bid = f"base{i}"
            s.register_batch(request_id=f"pb-{bid}", actor_id="eng1", boundary_id="eq1",
                             batch_id=bid, product_code="A",
                             started_at=f"2026-06-{i+1:02d}T06:00:00Z",
                             ended_at=f"2026-06-{i+1:02d}T14:00:00Z", output=1000.0)
            s.record_reading(request_id=f"pr-{bid}", actor_id="eng1", batch_id=bid,
                             meter_id="stm2", steam_kg=v,
                             measured_at=f"2026-06-{i+1:02d}T10:00:00Z")
        s.create_verification(
            request_id="vc-x", actor_id="eng1", boundary_id="eq1", verification_id="vx",
            title="分表核验",
            baseline_start="2026-06-01T00:00:00Z", baseline_end="2026-06-10T00:00:00Z",
            verification_start="2026-07-06T00:00:00Z", verification_end="2026-07-12T00:00:00Z")
        s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="vx")
        s.submit_for_review(request_id="sub", actor_id="eng1", verification_id="vx")
        s.review_verification(request_id="rv", actor_id="rev1", verification_id="vx",
                              decision="approve")
        # 复核发现证书有误，登记一份把有效期缩短到 2026-07-05 的更正证书，
        # 使验证期采用读数落入校准失效 → 自动暂停
        s.record_calibration(
            request_id="cal-fix", actor_id="a1", meter_id="stm2",
            certified_at="2026-01-01T00:00:00Z",
            valid_until="2026-07-05T00:00:00Z", certificate_ref="CORRECTED-CERT")
        self.assertEqual("suspended", s.get_verification("vx").status)

    def test_issue_window_not_touching_used_readings_does_not_suspend(self):
        s = self.service
        self._batches_and_readings([1000, 1010, 990, 1005, 995],
                                   [860, 850, 870, 855, 865])
        self._create_verification()
        s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="v1")
        s.submit_for_review(request_id="sub", actor_id="eng1", verification_id="v1")
        s.review_verification(request_id="rv", actor_id="rev1", verification_id="v1",
                              decision="approve")
        # 失效窗口位于核验窗口之外（5 月），不触及任何采用读数
        impact = s.report_meter_issue(
            request_id="iss", actor_id="eng1", meter_id="stm",
            issue_from="2026-05-01T00:00:00Z", issue_to="2026-05-10T00:00:00Z",
            note="核验窗口之前的检修问题")
        self.assertEqual([], impact["suspended_verifications"])
        self.assertEqual("confirmed", s.get_verification("v1").status)

    def test_idempotent_replay_does_not_duplicate(self):
        s = self.service
        self._batches_and_readings([1000, 1010, 990, 1005, 995],
                                   [860, 850, 870, 855, 865])
        self._create_verification()
        first = s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="v1")
        second = s.freeze_snapshot(request_id="frz", actor_id="eng1", verification_id="v1")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        valid, count = s.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
