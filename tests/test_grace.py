"""宽限期内的销毁拦截：列表、扫描卡片、分区详情、执行落库必须同源。"""
import os

from app.db import get_db
from app.services import execute_judgment, list_judgments, run_expire_scan
from app.services.grace import GraceBlocked, describe_partition


def _events_partition(authed):
    parts = authed.get("/api/partitions").json()["items"]
    return next(
        p
        for p in parts
        if p["domain_code"] == "events" and p["biz_date"] == "2026-11-12"
    )


def test_seeded_partition_is_in_grace_and_frozen(authed):
    part = _events_partition(authed)
    assert part["in_grace"] is True
    assert part["grace_until"] == "2026-11-20"
    assert part["days_left"] == 3
    assert part["can_destroy"] is False
    codes = {r["code"] for r in part["destroy_reasons"]}
    assert {"grace", "dependency", "backfill"} <= codes


def test_scan_card_reports_grace_blocked(authed):
    job = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-17"}).json()["job"]
    assert job["status"] == "已完成"
    assert job["expired_count"] >= 1
    assert job["grace_blocked_count"] >= 1
    assert "拦截" in job["message"]


def test_judgment_list_exposes_block_not_just_hiding_button(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-17"})
    items = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    blocked = [
        j
        for j in items
        if j["domain_code"] == "events" and j["decided_action"] == "销毁"
    ]
    assert blocked, "应有 events 域的销毁裁决"
    j = blocked[0]
    assert j["in_grace"] is True
    assert j["can_execute"] is False
    assert j["blocked"] is True
    assert j["block_reasons"]
    # 非宽限分区仍应可执行（不是一刀切全禁）
    assert any(item["can_execute"] for item in items)


def test_execute_inside_grace_rejected_without_side_effect(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-17"})
    items = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    target = next(
        j
        for j in items
        if j["domain_code"] == "events" and j["decided_action"] == "销毁"
    )
    before = _events_partition(authed)

    r = authed.post(f"/api/judgments/{target['id']}/execute")
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False
    assert body["disposition"] in ("reject", "freeze")
    assert body["block_reasons"]

    after = _events_partition(authed)
    assert after["storage_state"] == before["storage_state"] == "在线"
    assert after["row_count"] == before["row_count"] == 5200
    runs = authed.get("/api/runs").json()["items"]
    assert all(run["judgment_id"] != target["id"] for run in runs)
    remaining = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    assert any(j["id"] == target["id"] for j in remaining)


def test_archive_action_allowed_while_destroy_is_frozen(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-17"})
    items = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    archive = next(j for j in items if j["decided_action"] == "归档" and j["can_execute"])
    r = authed.post(f"/api/judgments/{archive['id']}/execute")
    assert r.status_code == 200
    assert r.json()["run"]["result_state"] == "已归档"


def test_partition_detail_and_grace_registrations(authed):
    part = _events_partition(authed)
    detail = authed.get(f"/api/partitions/{part['id']}").json()
    assert detail["partition"]["id"] == part["id"]
    assert detail["assessment"]["destroy"]["can_destroy"] is False
    assert detail["dependencies"], "分区详情应带依赖列表"
    assert detail["backfills"], "分区详情应带补采编排"
    assert detail["grace_registrations"], "应留下宽限登记流水"
    assert any(d["dep_type"] == "补采" for d in detail["dependencies"])


def test_backfill_completion_lifts_grace_but_keeps_other_blocks(authed):
    part = _events_partition(authed)
    detail = authed.get(f"/api/partitions/{part['id']}").json()
    bf = detail["backfills"][0]

    # 未完成不能标记完成
    r = authed.post(f"/api/backfills/{bf['id']}/complete")
    assert r.status_code == 409

    r = authed.post(
        f"/api/backfills/{bf['id']}/ingest", json={"ingested_rows": bf["planned_rows"]}
    )
    assert r.status_code == 200 and r.json()["backfill"]["progress_pct"] == 100.0

    r = authed.post(f"/api/backfills/{bf['id']}/complete")
    assert r.status_code == 200

    detail = authed.get(f"/api/partitions/{part['id']}").json()
    destroy = detail["assessment"]["destroy"]
    assert destroy["grace_until"] is None, "补采完成后宽限应同源清零"
    codes = {r["code"] for r in destroy["destroy_reasons"]}
    assert "grace" not in codes
    assert "dependency" in codes, "其余生效依赖仍应拦截"

    for dep in detail["dependencies"]:
        if dep["status"] == "生效":
            assert authed.post(f"/api/dependencies/{dep['id']}/release", json={}).status_code == 200

    destroy = authed.get(f"/api/partitions/{part['id']}").json()["assessment"]["destroy"]
    assert destroy["can_destroy"] is True


def test_grace_registration_is_exclusive_and_only_extends(authed):
    part = _events_partition(authed)

    # 先登记一个较长日期，再登记较短日期，不应把长日期盖回去
    assert authed.post(
        f"/api/partitions/{part['id']}/grace",
        json={"until": "2026-11-30", "reason": "长宽限", "mode": "extend"},
    ).status_code == 200
    short = authed.post(
        f"/api/partitions/{part['id']}/grace",
        json={"until": "2026-11-18", "reason": "短宽限", "mode": "extend"},
    )
    assert short.status_code == 200
    assert short.json()["manual_grace_until"] == "2026-11-30"

    # 覆盖式登记必须携带 expected_rev，且过期 rev 被拒
    detail = authed.get(f"/api/partitions/{part['id']}").json()
    stale = authed.post(
        f"/api/partitions/{part['id']}/grace",
        json={"until": "2026-11-18", "mode": "set", "expected_rev": 0},
    )
    assert stale.status_code == 409

    rev = detail["assessment"]["destroy"]["grace_rev"]
    fresh = authed.post(
        f"/api/partitions/{part['id']}/grace",
        json={"until": "2026-11-18", "mode": "set", "expected_rev": rev},
    )
    assert fresh.status_code == 200
    assert fresh.json()["manual_grace_until"] == "2026-11-18"


def test_reconcile_flags_no_grace_violation_after_blocked_attempt(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-17"})
    items = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    target = next(
        j
        for j in items
        if j["domain_code"] == "events" and j["decided_action"] == "销毁"
    )
    assert authed.post(f"/api/judgments/{target['id']}/execute").status_code == 409

    report = authed.post(
        "/api/reconcile-reports", json={"as_of_date": "2026-11-17"}
    ).json()["report"]
    assert report["grace_violations"] == []
    assert report["blocked_pending_count"] >= 1
    assert "宽限越权" in report["summary"]


def test_disposition_policies_are_mutually_exclusive(authed, monkeypatch):
    assert authed.get("/api/meta").json()["grace_disposition"] == "reject"
    monkeypatch.setenv("RETAIN_GRACE_DISPOSITION", "freeze")
    body = authed.get("/api/meta").json()
    assert body["grace_disposition"] == "freeze"
    assert body["grace_disposition_label"] == "只读冻结宽限登记"

    part = _events_partition(authed)
    assert part["can_destroy"] is False
    r = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-17"})
    assert r.status_code == 200
    items = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    target = next(
        j
        for j in items
        if j["domain_code"] == "events" and j["decided_action"] == "销毁"
    )
    assert authed.post(f"/api/judgments/{target['id']}/execute").status_code == 409

    monkeypatch.setenv("RETAIN_GRACE_DISPOSITION", "both")
    with get_db() as conn:
        with __import__("pytest").raises(RuntimeError):
            describe_partition(conn, conn.execute(
                "SELECT * FROM partitions WHERE id = ?", (part["id"],)
            ).fetchone(), action="销毁")


def test_freeze_disposition_registers_readonly_that_expires(authed, monkeypatch):
    monkeypatch.setenv("RETAIN_GRACE_DISPOSITION", "freeze")
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-17"})

    part = _events_partition(authed)
    assert part["frozen"] is True
    assert part["freeze_until"] == "2026-11-20"
    assert any(r["code"] == "grace" for r in part["destroy_reasons"])
    assert any(r["code"] == "read_only" for r in part["destroy_reasons"])

    # 只读冻结只拦销毁，不拦归档
    detail = authed.get(f"/api/partitions/{part['id']}").json()
    assert detail["assessment"]["archive"]["can_archive"] is True

    # 到期后冻结与宽限同时自动失效（剩余生效依赖仍在）
    with get_db() as conn:
        row = conn.execute("SELECT * FROM partitions WHERE id = ?", (part["id"],)).fetchone()
        later = describe_partition(conn, row, as_of="2026-11-21", action="销毁")
    assert later["frozen"] is False
    assert later["in_grace"] is False
    assert "read_only" not in {r["code"] for r in later["destroy_reasons"]}


def test_manual_readonly_with_until_expires(authed):
    part = _events_partition(authed)
    r = authed.post(
        f"/api/partitions/{part['id']}/read-only",
        json={"read_only": True, "until": "2026-11-18", "reason": "法务"},
    )
    assert r.status_code == 200
    assert r.json()["freeze_until"] == "2026-11-18"

    detail = authed.get(f"/api/partitions/{part['id']}").json()
    assert detail["assessment"]["destroy"]["frozen"] is True

    with get_db() as conn:
        row = conn.execute("SELECT * FROM partitions WHERE id = ?", (part["id"],)).fetchone()
        later = describe_partition(conn, row, as_of="2026-11-19", action="销毁")
    assert later["frozen"] is False


def test_backfill_orchestration_is_auditable(authed):
    part = _events_partition(authed)
    detail = authed.get(f"/api/partitions/{part['id']}").json()
    bf = detail["backfills"][0]
    assert detail["backfill_events"], "分区详情应带补采编排流水"

    events = authed.get("/api/backfill-events", params={"job_id": bf["id"]}).json()["items"]
    assert events and events[0]["action"] == "发起"

    authed.post(f"/api/backfills/{bf['id']}/ingest", json={"ingested_rows": bf["planned_rows"]})
    authed.post(f"/api/backfills/{bf['id']}/complete", json={})
    actions = [e["action"] for e in authed.get(
        "/api/backfill-events", params={"job_id": bf["id"]}
    ).json()["items"]]
    assert set(actions) >= {"发起", "上报", "完成"}
