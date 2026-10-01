"""蒸汽技改收益核验服务的离线端到端验收。

在临时 SQLite 数据库中走通：边界与计量点登记 → 基准/验证批次与蒸汽读数 →
批准调整因子 → 冻结快照 → 迟到读数不改变冻结结果 → 独立复核确认 →
计量失效暂停未结算收益 → 关闭期后只通过更正记录披露，并校验审计哈希链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from beverage_ops_foundation.storage import Database

from .clock import MutableClock
from .service import VerificationService


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "retrofit_acceptance.sqlite3")
        clock = MutableClock(datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
        s = VerificationService(database, clock)

        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="org-001", name="示范酒厂")
        s.register_actor(request_id="act-admin", actor_id="bootstrap", new_actor_id="admin-001",
                         display_name="管理员", role="admin", organization_id="org-001")
        s.register_actor(request_id="act-eng", actor_id="admin-001", new_actor_id="eng-001",
                         display_name="技改工程师", role="operator", organization_id="org-001")
        s.register_actor(request_id="act-rev", actor_id="admin-001", new_actor_id="rev-001",
                         display_name="独立复核员", role="reviewer", organization_id="org-001")
        s.register_site(request_id="site", actor_id="admin-001", site_id="site-001",
                        organization_id="org-001", name="一号蒸馏车间",
                        timezone_name="Asia/Shanghai")
        s.register_boundary(request_id="bnd", actor_id="eng-001", site_id="site-001",
                            boundary_id="eq-001", name="蒸汽冷凝水回用技改边界")
        s.register_meter_point(request_id="meter", actor_id="eng-001", boundary_id="eq-001",
                               meter_id="stm-001", name="蒸汽总表", metric="steam",
                               unit="kg", calibration_due="2027-01-01T00:00:00Z")

        baseline = [1000, 1012, 988, 1003, 997]
        verification = [862, 848, 871, 855, 864]
        for i, value in enumerate(baseline):
            bid = f"base-{i}"
            s.register_batch(request_id=f"pb-{bid}", actor_id="eng-001", boundary_id="eq-001",
                             batch_id=bid, product_code="Liquor-A",
                             started_at=f"2026-06-{i+1:02d}T06:00:00Z",
                             ended_at=f"2026-06-{i+1:02d}T14:00:00Z", output=1000.0)
            s.record_reading(request_id=f"pr-{bid}", actor_id="eng-001", batch_id=bid,
                             meter_id="stm-001", steam_kg=value,
                             measured_at=f"2026-06-{i+1:02d}T10:00:00Z")
        for i, value in enumerate(verification):
            bid = f"ver-{i}"
            s.register_batch(request_id=f"pb-{bid}", actor_id="eng-001", boundary_id="eq-001",
                             batch_id=bid, product_code="Liquor-A",
                             started_at=f"2026-07-{i+1:02d}T22:00:00Z",
                             ended_at=f"2026-07-{i+2:02d}T06:00:00Z", output=1000.0)
            s.record_reading(request_id=f"pr-{bid}", actor_id="eng-001", batch_id=bid,
                             meter_id="stm-001", steam_kg=value,
                             measured_at=f"2026-07-{i+1:02d}T23:00:00Z")

        # 复核角色批准产品结构调整因子（工程不能自批）
        s.register_adjustment_factor(request_id="factor", actor_id="rev-001",
                                     boundary_id="eq-001", code="product_mix",
                                     value=1.02, reason="验证期高度酒占比上升 +2%")

        s.create_verification(request_id="verification", actor_id="eng-001",
                              boundary_id="eq-001", verification_id="vr-001",
                              title="2026 蒸汽回用技改收益核验",
                              baseline_start="2026-06-01T00:00:00Z",
                              baseline_end="2026-06-10T00:00:00Z",
                              verification_start="2026-07-01T00:00:00Z",
                              verification_end="2026-07-10T00:00:00Z")
        s.freeze_snapshot(request_id="freeze", actor_id="eng-001", verification_id="vr-001")
        confirmed = s.get_verification("vr-001")
        saved_at_freeze = confirmed.result.steam_saved_kg

        # 跨班/跨月后迟到的修正读数：不进入冻结结果，只被标记
        clock.advance(timedelta(days=10))
        s.record_reading(request_id="late-reading", actor_id="eng-001", batch_id="base-0",
                         meter_id="stm-001", steam_kg=720.0,
                         measured_at="2026-06-01T10:00:00Z")
        quality = s.reading_quality("vr-001")
        replayed = s.replay_from_snapshot("vr-001")

        # 独立复核：提单人 eng-001 不能自审，由 rev-001 确认
        s.submit_for_review(request_id="submit", actor_id="eng-001",
                            verification_id="vr-001")
        s.review_verification(request_id="review", actor_id="rev-001",
                              verification_id="vr-001", decision="approve",
                              comment="测量链完整，置信区间下界为正，收益成立")

        # 计量失效波及已确认（未结算）收益：自动暂停，结算被阻断
        impact = s.report_meter_issue(
            request_id="issue-open", actor_id="eng-001", meter_id="stm-001",
            issue_from="2026-07-02T00:00:00Z", issue_to="2026-07-03T00:00:00Z",
            note="复核时发现短时校准漂移")
        suspended_status = s.get_verification("vr-001").status
        s.resolve_meter_issue(request_id="issue-resolve", actor_id="rev-001",
                              issue_id=impact["receipt"]["resource_id"])
        s.resume_verification(request_id="resume", actor_id="rev-001",
                              verification_id="vr-001", comment="重新校准合格")

        # 结算并关闭期间；关闭后再发现计量问题只能更正披露
        s.settle_benefit(request_id="settle", actor_id="admin-001",
                         verification_id="vr-001")
        s.close_period(request_id="close", actor_id="admin-001",
                       verification_id="vr-001")
        closed_impact = s.report_meter_issue(
            request_id="issue-closed", actor_id="rev-001", meter_id="stm-001",
            issue_from="2026-07-01T00:00:00Z", issue_to="2026-07-09T00:00:00Z",
            note="关账后审计发现历史漂移")
        s.record_correction(
            request_id="correction", actor_id="rev-001", verification_id="vr-001",
            issue_id=closed_impact["receipt"]["resource_id"],
            reason="历史校准漂移可能低估验证期蒸汽，仅披露不重算已结算收益",
            disclosed_impact={"estimated_overstatement_kg": 315.0,
                              "restated_unit_cost_change": 0.112})

        explanation = s.explain("vr-001")
        audit_valid, audit_events = s.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": audit_valid,
            "audit_events": audit_events,
            "verification_status": s.get_verification("vr-001").status,
            "suspended_on_issue": suspended_status == "suspended",
            "late_arrivals_flagged": len(quality["late_arrivals"]),
            "late_reading_changed_snapshot":
                abs(replayed.steam_saved_kg - saved_at_freeze) > 1e-9,
            "steam_saved_kg": round(saved_at_freeze, 3),
            "confidence_interval": [round(confirmed.result.interval_lower, 3),
                                    round(confirmed.result.interval_upper, 3)],
            "coverage_ratio": round(confirmed.result.coverage_ratio, 4),
            "effective_batches": len(explanation["effective_measurement_batches"]),
            "calibration_evidence": explanation["calibration_certificates_used"],
            "adjustment_factors": explanation["approved_adjustment_factors"],
            "closed_period_corrections": len(s.list_corrections("vr-001")),
            "closed_period_status_unchanged":
                s.get_verification("vr-001").status == "closed",
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["suspended_on_issue"]
          and result["late_arrivals_flagged"] == 1
          and result["late_reading_changed_snapshot"] is False
          and result["closed_period_status_unchanged"]
          and result["closed_period_corrections"] == 1
          and result["verification_status"] == "closed"
          and result["effective_batches"] == 10)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
