import sqlite3

from app.db import immediate_txn
from app.services.clock import parse_day
from app.services.grace import apply_freeze_policy, describe_partition
from app.services.policies import get_active_policy


def run_expire_scan(conn: sqlite3.Connection, as_of_date: str) -> dict:
    """扫描在线分区：超过策略保留天数则生成待执行裁决。

    宽限/依赖/补采判定与分区详情同源：仍在宽限期的分区照样开单，
    但裁决上标出不可执行，执行时再以同一判定拦截，避免「列表能点、点了能毁」。
    """
    as_of = parse_day(as_of_date)
    with immediate_txn(conn):
        cur = conn.execute(
            "INSERT INTO scan_jobs (as_of_date, status, message) VALUES (?,?,?)",
            (as_of_date, "进行中", ""),
        )
        job_id = int(cur.lastrowid)
        expired = 0
        grace_blocked = 0

        online = conn.execute(
            "SELECT * FROM partitions WHERE storage_state = '在线'"
        ).fetchall()

        for part in online:
            policy = get_active_policy(conn, part["domain_code"])
            if not policy:
                continue
            age = (as_of - parse_day(part["biz_date"])).days
            if age < int(policy["keep_days"]):
                continue
            # 同一分区若已有未完成裁决，不再重复开单（预留后续题：重裁/作废重开）
            exists = conn.execute(
                "SELECT id FROM judgments WHERE partition_id = ? "
                "AND status IN ('待执行','执行中')",
                (part["id"],),
            ).fetchone()
            if exists:
                continue
            assessment = describe_partition(
                conn,
                part,
                as_of=as_of_date,
                action=policy["action_on_expire"],
            )
            # 处置策略为 freeze：宽限中的分区落成只读冻结宽限登记（到期自动失效）。
            if assessment["in_grace"]:
                apply_freeze_policy(
                    conn,
                    part["id"],
                    until=assessment["grace_until"],
                    reason="到期扫描：宽限未到期，只读冻结宽限登记",
                )
            conn.execute(
                "INSERT INTO judgments "
                "(scan_job_id, partition_id, domain_code, biz_date, policy_version, "
                "keep_days, decided_action, status) "
                "VALUES (?,?,?,?,?,?,?, '待执行')",
                (
                    job_id,
                    part["id"],
                    part["domain_code"],
                    part["biz_date"],
                    policy["version"],
                    policy["keep_days"],
                    policy["action_on_expire"],
                ),
            )
            expired += 1
            if not assessment["executable"]:
                grace_blocked += 1

        if grace_blocked:
            message = (
                f"生成待执行裁决 {expired} 条，其中 {grace_blocked} 条受宽限/依赖/补采拦截"
            )
        else:
            message = f"生成待执行裁决 {expired} 条"
        conn.execute(
            "UPDATE scan_jobs SET status = ?, expired_count = ?, "
            "grace_blocked_count = ?, message = ? WHERE id = ?",
            ("已完成", expired, grace_blocked, message, job_id),
        )
    job = get_scan_job(conn, job_id)
    return job


def get_scan_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, as_of_date, status, expired_count, grace_blocked_count, "
        "message, created_at FROM scan_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def list_scan_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, as_of_date, status, expired_count, grace_blocked_count, "
        "message, created_at FROM scan_jobs ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


_COLUMNS = (
    "id, scan_job_id, partition_id, domain_code, biz_date, policy_version, "
    "keep_days, decided_action, status, fail_reason, created_at"
)


def list_judgments(
    conn: sqlite3.Connection,
    status: str | None = None,
    scan_job_id: int | None = None,
) -> list[dict]:
    """裁决清单：每条附带与分区详情同源的宽限判定与「可执行/拦截」结论。"""
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if scan_job_id is not None:
        clauses.append("scan_job_id = ?")
        params.append(scan_job_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT " + _COLUMNS + " FROM judgments" + where + " ORDER BY id DESC",
        params,
    ).fetchall()

    items: list[dict] = []
    for row in rows:
        item = dict(row)
        part = conn.execute(
            "SELECT * FROM partitions WHERE id = ?", (item["partition_id"],)
        ).fetchone()
        if part:
            assessment = describe_partition(
                conn, part, action=item["decided_action"]
            )
            item["grace"] = assessment
            item["in_grace"] = assessment["in_grace"]
            item["grace_until"] = assessment["grace_until"]
            item["days_left"] = assessment["days_left"]
            item["disposition"] = assessment["disposition"]
            item["disposition_label"] = assessment["disposition_label"]
            item["block_reasons"] = assessment["block_reasons"]
        else:
            item["grace"] = None
            item["in_grace"] = False
            item["grace_until"] = None
            item["days_left"] = None
            item["disposition"] = None
            item["disposition_label"] = None
            item["block_reasons"] = [{"code": "missing", "text": "分区不存在"}]
        open_state = item["status"] in ("待执行", "已失败")
        item["can_execute"] = open_state and not item["block_reasons"]
        item["blocked"] = open_state and bool(item["block_reasons"])
        items.append(item)
    return items
