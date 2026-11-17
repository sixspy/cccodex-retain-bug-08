"""宽限 / 依赖 / 补采治理。

本模块是「分区是否可销毁」的唯一判定源：扫描卡片、裁决列表、分区详情、
执行落库都调用 describe_partition()，避免页面与登记各说各话。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta

from app.db import immediate_txn
from app.services.clock import DATE_FMT, parse_day, today

# 两种互斥处置：只能二选一，不允许并存。
DISPOSITIONS: dict[str, str] = {
    "reject": "整单拒绝销毁",
    "freeze": "只读冻结宽限登记",
}
_DISPOSITION_ALIASES = {
    "拒毁": "reject",
    "整单拒绝销毁": "reject",
    "reject": "reject",
    "冻结": "freeze",
    "只读冻结": "freeze",
    "只读冻结宽限登记": "freeze",
    "freeze": "freeze",
}

DEFAULT_GRACE_DAYS = 3


class GraceBlocked(ValueError):
    """宽限/依赖/补采未完成导致销毁被拦。携带结构化信息给接口层。"""

    def __init__(self, message: str, *, disposition: str, grace: dict):
        super().__init__(message)
        self.message = message
        self.disposition = disposition
        self.grace = grace


def grace_disposition() -> str:
    """读取唯一生效的处置策略；配置非法时直接报错，绝不静默并存两种。"""
    raw = (os.environ.get("RETAIN_GRACE_DISPOSITION") or "reject").strip()
    key = _DISPOSITION_ALIASES.get(raw.lower()) or _DISPOSITION_ALIASES.get(raw)
    if key not in DISPOSITIONS:
        raise RuntimeError(
            f"RETAIN_GRACE_DISPOSITION 只能取 reject / freeze，当前为：{raw}"
        )
    return key


def disposition_label(key: str | None = None) -> str:
    return DISPOSITIONS[key or grace_disposition()]


def default_grace_days() -> int:
    raw = (os.environ.get("RETAIN_GRACE_DAYS") or "").strip()
    if not raw:
        return DEFAULT_GRACE_DAYS
    try:
        days = int(raw)
    except ValueError as exc:
        raise RuntimeError("RETAIN_GRACE_DAYS 必须是整数") from exc
    if days < 0:
        raise RuntimeError("RETAIN_GRACE_DAYS 不能为负数")
    return days


def _partition_row(conn: sqlite3.Connection, partition_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM partitions WHERE id = ?", (partition_id,)).fetchone()
    if not row:
        raise ValueError("分区不存在")
    return row


def _as_day(value: str | date | None) -> date:
    if value is None:
        return today()
    if isinstance(value, date):
        return value
    return parse_day(value)


def _max_day(values: list[str | None]) -> str | None:
    best: str | None = None
    for value in values:
        if not value:
            continue
        if best is None or parse_day(value) > parse_day(best):
            best = value
    return best


def effective_grace_until(conn: sqlite3.Connection, partition_id: int) -> str | None:
    """生效宽限截止日 = max(人工登记, 未完成补采的宽限)。与分区态同源。"""
    row = _partition_row(conn, partition_id)
    sources: list[str] = []
    if row["manual_grace_until"]:
        sources.append(row["manual_grace_until"])
    for bf in conn.execute(
        "SELECT grace_until FROM backfill_jobs WHERE partition_id = ? "
        "AND status IN ('未开始','进行中') AND grace_until IS NOT NULL",
        (partition_id,),
    ):
        sources.append(bf["grace_until"])
    return _max_day(sources)


def recompute_grace(conn: sqlite3.Connection, partition_id: int) -> str | None:
    """把同源计算出的截止日回写 partitions.grace_until（仅缓存，判定仍实时计算）。"""
    eff = effective_grace_until(conn, partition_id)
    conn.execute(
        "UPDATE partitions SET grace_until = ? WHERE id = ?", (eff, partition_id)
    )
    return eff


def _blocking_dependencies(conn: sqlite3.Connection, partition_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, dep_type, dep_ref, blocking, status, note, created_at, released_at "
        "FROM partition_dependencies WHERE partition_id = ? "
        "AND status = '生效' AND blocking = 1 ORDER BY id",
        (partition_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _incomplete_backfills(conn: sqlite3.Connection, partition_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, partition_id, domain_code, biz_date, status, planned_rows, "
        "ingested_rows, grace_days, grace_until, operator, note, created_at, updated_at "
        "FROM backfill_jobs WHERE partition_id = ? "
        "AND status IN ('未开始','进行中') "
        "AND ingested_rows < planned_rows ORDER BY id",
        (partition_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def describe_partition(
    conn: sqlite3.Connection,
    partition: sqlite3.Row | dict,
    *,
    as_of: str | date | None = None,
    action: str = "销毁",
) -> dict:
    """同源判定：宽限、只读、依赖、补采 → 当前动作是否可执行。"""
    as_of_day = _as_day(as_of)
    pid = int(partition["id"])
    # 只读/冻结状态以库内当前值为准，避免调用方传入的旧快照导致判定偏差。
    fresh = conn.execute(
        "SELECT read_only, freeze_until FROM partitions WHERE id = ?", (pid,)
    ).fetchone()
    read_only = bool(fresh["read_only"]) if fresh else False
    freeze_until = fresh["freeze_until"] if fresh else None
    eff = effective_grace_until(conn, pid)
    in_grace = bool(eff) and as_of_day <= parse_day(eff)
    days_left = (parse_day(eff) - as_of_day).days if eff else None
    # 有限期冻结到期自动失效；未设截止日的只读冻结须人工解除。
    if freeze_until:
        frozen = as_of_day <= parse_day(freeze_until)
    else:
        frozen = read_only

    blocking_deps = _blocking_dependencies(conn, pid)
    incomplete_bf = _incomplete_backfills(conn, pid)
    disposition = grace_disposition()

    destroy_reasons: list[dict] = []
    if partition["storage_state"] not in ("在线", "裁撤中"):
        destroy_reasons.append(
            {
                "code": "state",
                "text": f"分区状态不可裁撤：{partition['storage_state']}",
            }
        )
    if in_grace:
        if disposition == "freeze":
            destroy_reasons.append(
                {
                    "code": "grace",
                    "text": (
                        f"仍在宽限期至 {eff}（剩余 {days_left} 天）："
                        "只读冻结宽限登记，禁止销毁"
                    ),
                }
            )
        else:
            destroy_reasons.append(
                {
                    "code": "grace",
                    "text": f"仍在宽限期至 {eff}（剩余 {days_left} 天），整单拒绝销毁",
                }
            )
    if frozen:
        destroy_reasons.append(
            {
                "code": "read_only",
                "text": f"分区处于只读冻结（至 {freeze_until or '人工解除'}），禁止销毁",
            }
        )
    for dep in blocking_deps:
        destroy_reasons.append(
            {
                "code": "dependency",
                "text": f"生效依赖 {dep['dep_type']}：{dep['dep_ref']} 未解除",
            }
        )
    for bf in incomplete_bf:
        destroy_reasons.append(
            {
                "code": "backfill",
                "text": (
                    f"补采任务#{bf['id']} 入库未完成 "
                    f"({bf['ingested_rows']}/{bf['planned_rows']})"
                ),
            }
        )

    archive_reasons: list[dict] = []
    if partition["storage_state"] not in ("在线", "裁撤中"):
        archive_reasons.append(
            {
                "code": "state",
                "text": f"分区状态不可裁撤：{partition['storage_state']}",
            }
        )

    reasons = destroy_reasons if action == "销毁" else archive_reasons
    executable = not reasons

    return {
        "partition_id": pid,
        "domain_code": partition["domain_code"],
        "biz_date": partition["biz_date"],
        "storage_state": partition["storage_state"],
        "row_count": partition["row_count"],
        "archive_uri": partition["archive_uri"],
        "grace_until": eff,
        "grace_persisted": partition["grace_until"]
        if "grace_until" in partition.keys()
        else None,
        "manual_grace_until": partition["manual_grace_until"]
        if "manual_grace_until" in partition.keys()
        else None,
        "in_grace": in_grace,
        "days_left": days_left,
        "read_only": read_only,
        "freeze_until": freeze_until,
        "frozen": frozen,        "grace_rev": int(partition["grace_rev"])
        if "grace_rev" in partition.keys()
        else 0,
        "disposition": disposition,
        "disposition_label": DISPOSITIONS[disposition],
        "blocking_dependencies": blocking_deps,
        "incomplete_backfills": incomplete_bf,
        "can_destroy": not destroy_reasons,
        "destroy_reasons": destroy_reasons,
        "can_archive": not archive_reasons,
        "archive_reasons": archive_reasons,
        "assessed_action": action,
        "executable": executable,
        "block_reasons": reasons,
    }


# --------------------------------------------------------------------------
# 宽限登记（排他写）
# --------------------------------------------------------------------------


def _register_grace_locked(
    conn: sqlite3.Connection,
    partition_id: int,
    until: str,
    *,
    operator: str,
    reason: str,
    expected_rev: int | None = None,
    mode: str = "extend",
) -> dict:
    """调用方必须已持有写事务。"""
    part = _partition_row(conn, partition_id)
    try:
        until_day = parse_day(until)
    except ValueError as exc:
        raise ValueError(f"宽限截止日格式应为 YYYY-MM-DD：{until}") from exc
    if until_day < today():
        raise ValueError("宽限截止日不能早于今天")

    if expected_rev is not None and int(part["grace_rev"]) != int(expected_rev):
        raise ValueError(
            f"宽限登记已被他人更新（期望 rev={expected_rev}，实际 rev={part['grace_rev']}）"
        )
    if mode not in ("extend", "set"):
        raise ValueError("mode 只能是 extend 或 set")
    if mode == "set" and expected_rev is None:
        raise ValueError("覆盖式登记必须携带 expected_rev，防止互盖")

    current = part["manual_grace_until"]
    if mode == "extend" and current and parse_day(current) > until_day:
        # 并发抢推下只增不减：后来的短日期不能把别人的长日期盖回去。
        effective = current
    else:
        effective = until_day.isoformat()

    conn.execute(
        "UPDATE partitions SET manual_grace_until = ?, grace_rev = grace_rev + 1 "
        "WHERE id = ?",
        (effective, partition_id),
    )
    eff = recompute_grace(conn, partition_id)
    conn.execute(
        "INSERT INTO grace_registrations "
        "(partition_id, action, requested_until, effective_until, operator, reason) "
        "VALUES (?,?,?,?,?,?)",
        (partition_id, "登记", until, eff or "", operator, reason),
    )
    return {"grace_until": eff, "manual_grace_until": effective}


def register_grace(
    conn: sqlite3.Connection,
    partition_id: int,
    until: str,
    *,
    operator: str = "",
    reason: str = "",
    expected_rev: int | None = None,
    mode: str = "extend",
) -> dict:
    """人工登记/延长宽限。BEGIN IMMEDIATE 排他写，读-改-写不丢更新。"""
    with immediate_txn(conn):
        return _register_grace_locked(
            conn,
            partition_id,
            until,
            operator=operator,
            reason=reason,
            expected_rev=expected_rev,
            mode=mode,
        )


def release_grace(
    conn: sqlite3.Connection,
    partition_id: int,
    *,
    operator: str = "",
    reason: str = "",
) -> dict:
    with immediate_txn(conn):
        _partition_row(conn, partition_id)
        conn.execute(
            "UPDATE partitions SET manual_grace_until = NULL, "
            "grace_rev = grace_rev + 1 WHERE id = ?",
            (partition_id,),
        )
        eff = recompute_grace(conn, partition_id)
        conn.execute(
            "INSERT INTO grace_registrations "
            "(partition_id, action, requested_until, effective_until, operator, reason) "
            "VALUES (?,?,?,?,?,?)",
            (partition_id, "解除", "", eff or "", operator, reason),
        )
        return {"grace_until": eff}


def set_read_only(
    conn: sqlite3.Connection,
    partition_id: int,
    read_only: bool,
    *,
    reason: str = "",
    until: str | None = None,
) -> dict:
    """只读冻结。until 为空时视为人工长期冻结（直到显式解除）。"""
    if until is not None:
        parse_day(until)  # 校验格式
    with immediate_txn(conn):
        _partition_row(conn, partition_id)
        conn.execute(
            "UPDATE partitions SET read_only = ?, freeze_until = ? WHERE id = ?",
            (1 if read_only else 0, until if read_only else None, partition_id),
        )
        if read_only:
            conn.execute(
                "INSERT INTO grace_registrations "
                "(partition_id, action, requested_until, effective_until, operator, reason) "
                "VALUES (?,?,?,?,?,?)",
                (
                    partition_id,
                    "重算",
                    until or "",
                    recompute_grace(conn, partition_id) or "",
                    "system",
                    reason or "只读冻结",
                ),
            )
        return {"read_only": bool(read_only), "freeze_until": until if read_only else None}


def list_grace_registrations(
    conn: sqlite3.Connection, partition_id: int | None = None
) -> list[dict]:
    if partition_id is None:
        rows = conn.execute(
            "SELECT id, partition_id, action, requested_until, effective_until, "
            "operator, reason, created_at FROM grace_registrations ORDER BY id DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, partition_id, action, requested_until, effective_until, "
            "operator, reason, created_at FROM grace_registrations "
            "WHERE partition_id = ? ORDER BY id DESC",
            (partition_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def apply_freeze_policy(
    conn: sqlite3.Connection,
    partition_id: int,
    *,
    until: str | None,
    operator: str = "system",
    reason: str = "宽限处置：只读冻结宽限登记",
) -> dict | None:
    """处置策略为 freeze 时，把宽限中的分区登记为只读冻结（到期自动失效）。

    必须已持有写事务。重复调用幂等。
    """
    if grace_disposition() != "freeze":
        return None
    row = _partition_row(conn, partition_id)
    if row["storage_state"] not in ("在线", "裁撤中"):
        return None
    if row["read_only"] and row["freeze_until"] == until:
        return None
    conn.execute(
        "UPDATE partitions SET read_only = 1, freeze_until = ? WHERE id = ?",
        (until, partition_id),
    )
    conn.execute(
        "INSERT INTO grace_registrations "
        "(partition_id, action, requested_until, effective_until, operator, reason) "
        "VALUES (?,?,?,?,?,?)",
        (partition_id, "登记", until or "", recompute_grace(conn, partition_id) or "", operator, reason),
    )
    return {"read_only": True, "freeze_until": until}


# --------------------------------------------------------------------------
# 依赖列表
# --------------------------------------------------------------------------

def list_dependencies(
    conn: sqlite3.Connection, partition_id: int | None = None
) -> list[dict]:
    sql = (
        "SELECT d.id, d.partition_id, p.domain_code, p.biz_date, d.dep_type, "
        "d.dep_ref, d.blocking, d.status, d.note, d.created_at, d.released_at "
        "FROM partition_dependencies d JOIN partitions p ON p.id = d.partition_id"
    )
    params: list[object] = []
    if partition_id is not None:
        sql += " WHERE d.partition_id = ?"
        params.append(partition_id)
    sql += " ORDER BY d.status, d.id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def add_dependency(
    conn: sqlite3.Connection,
    partition_id: int,
    *,
    dep_type: str,
    dep_ref: str,
    blocking: bool = True,
    note: str = "",
) -> dict:
    if dep_type not in ("补采", "下游消费", "跨域引用"):
        raise ValueError("依赖类型只能是 补采 / 下游消费 / 跨域引用")
    if not dep_ref.strip():
        raise ValueError("缺少依赖引用")
    with immediate_txn(conn):
        _partition_row(conn, partition_id)
        cur = conn.execute(
            "INSERT INTO partition_dependencies "
            "(partition_id, dep_type, dep_ref, blocking, status, note) "
            "VALUES (?,?,?,?, '生效', ?)",
            (partition_id, dep_type, dep_ref.strip(), 1 if blocking else 0, note),
        )
        dep_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT id, partition_id, dep_type, dep_ref, blocking, status, note, "
            "created_at, released_at FROM partition_dependencies WHERE id = ?",
            (dep_id,),
        ).fetchone()
        return dict(row)


def release_dependency(conn: sqlite3.Connection, dep_id: int, *, note: str = "") -> dict:
    with immediate_txn(conn):
        row = conn.execute(
            "SELECT * FROM partition_dependencies WHERE id = ?", (dep_id,)
        ).fetchone()
        if not row:
            raise ValueError("依赖不存在")
        if row["status"] == "已解除":
            return dict(row)
        conn.execute(
            "UPDATE partition_dependencies SET status = '已解除', "
            "released_at = datetime('now'), note = CASE WHEN ? = '' THEN note ELSE ? END "
            "WHERE id = ?",
            (note, note, dep_id),
        )
        updated = conn.execute(
            "SELECT id, partition_id, dep_type, dep_ref, blocking, status, note, "
            "created_at, released_at FROM partition_dependencies WHERE id = ?",
            (dep_id,),
        ).fetchone()
        return dict(updated)


# --------------------------------------------------------------------------
# 补采编排
# --------------------------------------------------------------------------


def list_backfills(
    conn: sqlite3.Connection, partition_id: int | None = None
) -> list[dict]:
    sql = (
        "SELECT b.id, b.partition_id, b.domain_code, b.biz_date, b.status, "
        "b.planned_rows, b.ingested_rows, b.grace_days, b.grace_until, b.operator, "
        "b.note, b.created_at, b.updated_at, "
        "CASE WHEN b.planned_rows > 0 THEN "
        "  ROUND(b.ingested_rows * 100.0 / b.planned_rows, 1) ELSE 0 END AS progress_pct, "
        "(b.ingested_rows < b.planned_rows) AS incomplete "
        "FROM backfill_jobs b"
    )
    params: list[object] = []
    if partition_id is not None:
        sql += " WHERE b.partition_id = ?"
        params.append(partition_id)
    sql += " ORDER BY b.id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _backfill_row(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM backfill_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if not row:
        raise ValueError("补采任务不存在")
    return row


def create_backfill(
    conn: sqlite3.Connection,
    partition_id: int,
    *,
    planned_rows: int | None = None,
    grace_days: int | None = None,
    operator: str = "",
    note: str = "",
) -> dict:
    """发起补采：登记宽限，并自动挂一条 blocking 补采依赖。

    补采入库未完成期间，销毁一律被同源判定拦截。
    """
    days = default_grace_days() if grace_days is None else int(grace_days)
    if days < 0:
        raise ValueError("宽限天数不能为负数")
    with immediate_txn(conn):
        part = _partition_row(conn, partition_id)
        if part["storage_state"] not in ("在线", "裁撤中"):
            raise ValueError(f"分区状态不可补采：{part['storage_state']}")
        open_job = conn.execute(
            "SELECT id FROM backfill_jobs WHERE partition_id = ? "
            "AND status IN ('未开始','进行中')",
            (partition_id,),
        ).fetchone()
        if open_job:
            raise ValueError(f"该分区已有进行中的补采任务#{open_job['id']}")
        planned = int(part["row_count"]) if planned_rows is None else int(planned_rows)
        if planned < 0:
            raise ValueError("计划行数不能为负数")
        grace_until = (today() + timedelta(days=days)).isoformat()
        cur = conn.execute(
            "INSERT INTO backfill_jobs "
            "(partition_id, domain_code, biz_date, status, planned_rows, "
            "ingested_rows, grace_days, grace_until, operator, note) "
            "VALUES (?,?,?, '进行中', ?,0,?,?,?,?)",
            (
                partition_id,
                part["domain_code"],
                part["biz_date"],
                planned,
                days,
                grace_until,
                operator,
                note,
            ),
        )
        job_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO partition_dependencies "
            "(partition_id, dep_type, dep_ref, blocking, status, note) "
            "VALUES (?, '补采', ?, 1, '生效', ?)",
            (partition_id, f"backfill#{job_id}", "补采入库未完成，禁止销毁"),
        )
        eff = recompute_grace(conn, partition_id)
        _log_backfill_event(
            conn,
            job_id,
            partition_id,
            "发起",
            ingested_rows=0,
            planned_rows=planned,
            grace_until=eff,
            operator=operator,
            note=note,
        )
        return _backfill_dict(conn, job_id)


def _log_backfill_event(
    conn: sqlite3.Connection,
    job_id: int,
    partition_id: int,
    action: str,
    *,
    ingested_rows: int = 0,
    planned_rows: int = 0,
    grace_until: str | None = None,
    operator: str = "",
    note: str = "",
) -> None:
    conn.execute(
        "INSERT INTO backfill_events "
        "(backfill_id, partition_id, action, ingested_rows, planned_rows, "
        "grace_until, operator, note) VALUES (?,?,?,?,?,?,?,?)",
        (
            job_id,
            partition_id,
            action,
            ingested_rows,
            planned_rows,
            grace_until,
            operator,
            note,
        ),
    )


def list_backfill_events(
    conn: sqlite3.Connection, job_id: int | None = None
) -> list[dict]:
    sql = (
        "SELECT id, backfill_id, partition_id, action, ingested_rows, planned_rows, "
        "grace_until, operator, note, created_at FROM backfill_events"
    )
    params: list[object] = []
    if job_id is not None:
        sql += " WHERE backfill_id = ?"
        params.append(job_id)
    sql += " ORDER BY id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _backfill_dict(conn: sqlite3.Connection, job_id: int) -> dict:
    rows = list_backfills(conn)
    for row in rows:
        if row["id"] == job_id:
            return row
    raise ValueError("补采任务不存在")


def ingest_backfill(
    conn: sqlite3.Connection, job_id: int, *, ingested_rows: int, operator: str = ""
) -> dict:
    """上报入库进度。排他写，进度只增不减（并发上报不会回退）。"""
    with immediate_txn(conn):
        job = _backfill_row(conn, job_id)
        if job["status"] != "进行中":
            raise ValueError(f"当前补采状态不可上报：{job['status']}")
        new_rows = int(ingested_rows)
        if new_rows < int(job["ingested_rows"]):
            raise ValueError(
                f"入库行数不能回退（当前 {job['ingested_rows']}，上报 {new_rows}）"
            )
        if int(job["planned_rows"]) and new_rows > int(job["planned_rows"]):
            raise ValueError(f"入库行数不能超过计划 {job['planned_rows']}")
        conn.execute(
            "UPDATE backfill_jobs SET ingested_rows = ?, operator = CASE WHEN ? = '' "
            "THEN operator ELSE ? END, updated_at = datetime('now') WHERE id = ?",
            (new_rows, operator, operator, job_id),
        )
        # 补采若被手动延期，宽限取原截止日与今天+宽限天数中的较晚者。
        if job["grace_until"] and parse_day(job["grace_until"]) < today():
            extended = (today() + timedelta(days=int(job["grace_days"]))).isoformat()
            conn.execute(
                "UPDATE backfill_jobs SET grace_until = ? WHERE id = ?",
                (extended, job_id),
            )
        eff = recompute_grace(conn, int(job["partition_id"]))
        _log_backfill_event(
            conn,
            job_id,
            int(job["partition_id"]),
            "上报",
            ingested_rows=new_rows,
            planned_rows=int(job["planned_rows"]),
            grace_until=eff,
            operator=operator,
            note="",
        )
        return _backfill_dict(conn, job_id)


def complete_backfill(
    conn: sqlite3.Connection, job_id: int, *, operator: str = "", note: str = ""
) -> dict:
    with immediate_txn(conn):
        job = _backfill_row(conn, job_id)
        if job["status"] not in ("未开始", "进行中"):
            raise ValueError(f"当前补采状态不可完成：{job['status']}")
        if int(job["ingested_rows"]) < int(job["planned_rows"]):
            raise ValueError(
                f"补采入库未完成（{job['ingested_rows']}/{job['planned_rows']}），"
                "不能标记完成"
            )
        conn.execute(
            "UPDATE backfill_jobs SET status = '已完成', note = CASE WHEN ? = '' "
            "THEN note ELSE ? END, updated_at = datetime('now') WHERE id = ?",
            (note, note, job_id),
        )
        conn.execute(
            "UPDATE partition_dependencies SET status = '已解除', "
            "released_at = datetime('now'), note = '补采完成自动解除' "
            "WHERE partition_id = ? AND dep_type = '补采' "
            "AND dep_ref = ? AND status = '生效'",
            (job["partition_id"], f"backfill#{job_id}"),
        )
        eff = recompute_grace(conn, int(job["partition_id"]))
        _log_backfill_event(
            conn,
            job_id,
            int(job["partition_id"]),
            "完成",
            ingested_rows=int(job["ingested_rows"]),
            planned_rows=int(job["planned_rows"]),
            grace_until=eff,
            operator=operator,
            note=note,
        )
        return _backfill_dict(conn, job_id)


def abort_backfill(
    conn: sqlite3.Connection, job_id: int, *, operator: str = "", note: str = ""
) -> dict:
    with immediate_txn(conn):
        job = _backfill_row(conn, job_id)
        if job["status"] not in ("未开始", "进行中"):
            raise ValueError(f"当前补采状态不可中止：{job['status']}")
        conn.execute(
            "UPDATE backfill_jobs SET status = '已中止', note = CASE WHEN ? = '' "
            "THEN note ELSE ? END, updated_at = datetime('now') WHERE id = ?",
            (note, note, job_id),
        )
        conn.execute(
            "UPDATE partition_dependencies SET status = '已解除', "
            "released_at = datetime('now'), note = '补采中止自动解除' "
            "WHERE partition_id = ? AND dep_type = '补采' "
            "AND dep_ref = ? AND status = '生效'",
            (job["partition_id"], f"backfill#{job_id}"),
        )
        eff = recompute_grace(conn, int(job["partition_id"]))
        _log_backfill_event(
            conn,
            job_id,
            int(job["partition_id"]),
            "中止",
            ingested_rows=int(job["ingested_rows"]),
            planned_rows=int(job["planned_rows"]),
            grace_until=eff,
            operator=operator,
            note=note,
        )
        return _backfill_dict(conn, job_id)
