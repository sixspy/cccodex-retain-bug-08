import sqlite3

from app.services.grace import describe_partition


def run_reconcile(conn: sqlite3.Connection, as_of_date: str) -> dict:
    """策略视角与实物分区对账：统计各存储态，并找出错位与宽限不一致。"""
    counts = {
        r["storage_state"]: r["c"]
        for r in conn.execute(
            "SELECT storage_state, COUNT(*) AS c FROM partitions GROUP BY storage_state"
        ).fetchall()
    }
    online = int(counts.get("在线", 0))
    archived = int(counts.get("已归档", 0))
    destroyed = int(counts.get("已销毁", 0))
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM judgments WHERE status IN ('待执行','执行中')"
    ).fetchone()["c"]

    mismatches = conn.execute(
        """
        SELECT j.id AS judgment_id, j.decided_action, j.status AS judgment_status,
               p.id AS partition_id, p.storage_state
        FROM judgments j
        JOIN partitions p ON p.id = j.partition_id
        WHERE j.status = '已完成'
          AND (
            (j.decided_action = '归档' AND p.storage_state != '已归档')
            OR (j.decided_action = '销毁' AND p.storage_state != '已销毁')
          )
        """
    ).fetchall()
    mismatch_count = len(mismatches)

    # 宽限一致性：分区登记了宽限却已被销毁，属于必须报警的错位。
    grace_violations = conn.execute(
        """
        SELECT p.id AS partition_id, p.domain_code, p.biz_date, p.grace_until,
               p.storage_state
        FROM partitions p
        WHERE p.storage_state = '已销毁'
          AND p.grace_until IS NOT NULL
          AND p.grace_until >= ?
        """,
        (as_of_date,),
    ).fetchall()

    # 待执行裁决里当前被宽限/依赖/补采拦截的条数（同源判定）。
    pending_rows = conn.execute(
        "SELECT j.id AS judgment_id, j.decided_action, j.status AS judgment_status, "
        "p.id, p.domain_code, p.biz_date, p.row_count, p.storage_state, "
        "p.archive_uri, p.grace_until, p.manual_grace_until, p.read_only, p.grace_rev "
        "FROM judgments j JOIN partitions p ON p.id = j.partition_id "
        "WHERE j.status IN ('待执行','已失败')"
    ).fetchall()
    blocked_pending = 0
    for row in pending_rows:
        assessment = describe_partition(
            conn, row, as_of=as_of_date, action=row["decided_action"]
        )
        if not assessment["executable"]:
            blocked_pending += 1

    summary = (
        f"在线{online} / 已归档{archived} / 已销毁{destroyed} / "
        f"待执行裁决{pending} / 错位{mismatch_count} / "
        f"宽限拦截{blocked_pending} / 宽限越权{len(grace_violations)}"
    )
    cur = conn.execute(
        "INSERT INTO reconcile_reports "
        "(as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, summary) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            as_of_date,
            online,
            archived,
            destroyed,
            int(pending),
            mismatch_count + len(grace_violations),
            summary,
        ),
    )
    report_id = int(cur.lastrowid)
    row = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, summary, created_at "
        "FROM reconcile_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    report = dict(row)
    report["mismatches"] = [dict(m) for m in mismatches]
    report["grace_violations"] = [dict(g) for g in grace_violations]
    report["blocked_pending_count"] = blocked_pending
    return report


def list_reports(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, summary, created_at "
        "FROM reconcile_reports ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]
