"""运行基础服务与技改收益核验的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock, ManualClock
from .retrofit_service import RetrofitService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行完整登记链与技改核验链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范经营主体")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="业务负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="独立复核人", role="reviewer", organization_id="org-001")
        service.register_actor(request_id="req-auditor", actor_id="admin-001", new_actor_id="auditor-001",
                               display_name="审计员", role="auditor", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产经营站点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="company_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="company_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        retrofit_summary = _run_retrofit(database)

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed, "retrofit": retrofit_summary}
        database.close()
        return result


def _run_retrofit(database: Database) -> dict[str, object]:
    """在可推进时钟上走完登记、冻结、复核、关闭、失效暂停与更正披露。"""

    clock = ManualClock(datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc))
    service = DomainService(database, clock)
    retro = RetrofitService(database, clock)

    retro.register_boundary(request_id="rt-boundary", actor_id="operator-001", boundary_id="line-1",
                            site_id="site-001", name="蒸馏蒸汽技改边界",
                            scope={"equipment": ["boiler-1", "distillation-column"]})
    retro.register_meter(request_id="rt-meter-s", actor_id="operator-001", meter_id="steam-main",
                         boundary_id="line-1", meter_type="steam", unit="t", min_range=0, max_range=500)
    retro.register_meter(request_id="rt-meter-o", actor_id="operator-001", meter_id="output-main",
                         boundary_id="line-1", meter_type="output", unit="hl", min_range=0, max_range=5000)
    retro.register_calibration(request_id="rt-cal-s", actor_id="operator-001", calibration_id="cal-steam",
                               meter_id="steam-main", valid_from="2025-12-01T00:00:00Z",
                               valid_until="2026-12-31T23:59:59Z", certificate_no="STEAM-CERT-01")
    retro.register_calibration(request_id="rt-cal-o", actor_id="operator-001", calibration_id="cal-output",
                               meter_id="output-main", valid_from="2025-12-01T00:00:00Z",
                               valid_until="2026-12-31T23:59:59Z", certificate_no="OUTPUT-CERT-01")

    batches = [
        ("BASE-1", "baseline", "01", 10, "早班", 120.0, 100.0),
        ("BASE-2", "baseline", "01", 11, "中班", 124.0, 100.0),
        ("BASE-3", "baseline", "01", 12, "晚班", 118.0, 100.0),
        ("VER-1", "verification", "03", 10, "早班", 98.0, 100.0),
        ("VER-2", "verification", "03", 11, "中班", 100.0, 100.0),
        ("VER-3", "verification", "03", 12, "晚班", 96.0, 100.0),
    ]
    for bid, kind, month, day, shift, steam, output in batches:
        started = f"2026-{month}-{day}T00:00:00Z"
        retro.register_batch(request_id=f"rt-batch-{bid}", actor_id="operator-001", batch_id=bid,
                             boundary_id="line-1", product_code="liquor-A", period_kind=kind,
                             shift_name=shift, started_at=started,
                             ended_at=f"2026-{month}-{day}T08:00:00Z", output=output,
                             steam_unit_cost=100.0, downtime_minutes=30 if bid == "BASE-2" else 0)
        retro.record_reading(request_id=f"rt-rs-{bid}", actor_id="operator-001", meter_id="steam-main",
                             batch_id=bid, observed_at=started, value=steam)
        retro.record_reading(request_id=f"rt-ro-{bid}", actor_id="operator-001", meter_id="output-main",
                             batch_id=bid, observed_at=started, value=output)

    # 产品结构调整因子：工程提议，独立复核者批准。
    retro.propose_adjustment_factor(request_id="rt-factor", actor_id="operator-001", factor_id="factor-1",
                                    boundary_id="line-1", period_kind="baseline", factor=0.97,
                                    reason="验证期高度酒占比上升的结构归一化")
    retro.review_adjustment_factor(request_id="rt-factor-review", actor_id="reviewer-001",
                                   factor_id="factor-1", decision="approved", note="结构表核对一致")

    frozen = retro.freeze_verification(
        request_id="rt-freeze", actor_id="operator-001", boundary_id="line-1", name="2026年一季度蒸汽技改核验",
        baseline_start="2026-01-01T00:00:00Z", baseline_end="2026-01-31T23:59:59Z",
        verification_start="2026-03-01T00:00:00Z", verification_end="2026-03-31T23:59:59Z")
    verification_id = frozen.resource_id
    explanation = retro.explain(verification_id)
    results = explanation["results"]
    if results is None:
        raise RuntimeError("有效测量充足时应当产出计算结果")

    retro.confirm_verification(request_id="rt-confirm", actor_id="reviewer-001",
                               verification_id=verification_id, note="数据链与调整因子复核通过")
    retro.close_verification(request_id="rt-close", actor_id="reviewer-001",
                             verification_id=verification_id, note="一季度期间关闭结算")

    # 已关闭期间发现蒸汽校准在验证期初失效：只能通过更正记录披露，状态保持 closed。
    clock.advance(days=10)
    correction = retro.report_calibration_failure(
        request_id="rt-correction", actor_id="auditor-001", meter_id="steam-main",
        effective_from="2026-03-01T00:00:00Z", reason="复查发现校准证书签章不符",
        calibration_id="cal-steam")
    closed_view = retro.get_verification(verification_id)
    impacts = closed_view["calibration_impacts"]
    if closed_view["status"] != "closed" or not impacts or impacts[0]["action"] != "disclosure":
        raise RuntimeError("已关闭期间的校准失效必须只做更正披露")

    return {
        "boundary_id": "line-1",
        "verification_id": verification_id,
        "snapshot_hash": explanation["snapshot_hash"],
        "sufficient_data": explanation["sufficient_data"],
        "baseline_count": results["baseline_count"],
        "verification_count": results["verification_count"],
        "savings_steam": results["savings_steam"],
        "savings_amount": results["savings_amount"],
        "unit_cost_delta": results["unit_cost_delta"],
        "rate_delta_interval": [results["rate_delta_low"], results["rate_delta_high"]],
        "final_status": closed_view["status"],
        "correction_id": correction.resource_id,
        "disclosed_at_risk": impacts[0]["savings_amount_at_risk"],
        "effective_measurements": len(explanation["effective_measurements"]),
    }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
