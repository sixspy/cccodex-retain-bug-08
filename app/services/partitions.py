import sqlite3

_COLUMNS = (
    "id, domain_code, biz_date, row_count, storage_state, archive_uri, "
    "grace_until, manual_grace_until, read_only, grace_rev, created_at"
)


def _rows(conn: sqlite3.Connection, sql: str, params: list[object]) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_partitions(
    conn: sqlite3.Connection,
    domain_code: str | None = None,
    storage_state: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if domain_code:
        clauses.append("domain_code = ?")
        params.append(domain_code)
    if storage_state:
        clauses.append("storage_state = ?")
        params.append(storage_state)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return _rows(
        conn,
        "SELECT " + _COLUMNS + " FROM partitions" + where
        + " ORDER BY domain_code, biz_date",
        params,
    )


def get_partition(conn: sqlite3.Connection, partition_id: int) -> dict | None:
    rows = _rows(
        conn,
        "SELECT " + _COLUMNS + " FROM partitions WHERE id = ?",
        [partition_id],
    )
    return rows[0] if rows else None


def get_partition_detail(conn: sqlite3.Connection, partition_id: int) -> dict | None:
    """分区详情：分区态 + 同源可执行判定 + 依赖列表 + 补采编排。"""
    from app.services.grace import (
        describe_partition,
        list_backfill_events,
        list_backfills,
        list_dependencies,
    )

    part = get_partition(conn, partition_id)
    if not part:
        return None
    return {
        "partition": part,
        "assessment": {
            "destroy": describe_partition(conn, part, action="销毁"),
            "archive": describe_partition(conn, part, action="归档"),
        },
        "dependencies": list_dependencies(conn, partition_id),
        "backfills": list_backfills(conn, partition_id),
        "backfill_events": [
            event
            for bf in list_backfills(conn, partition_id)
            for event in list_backfill_events(conn, bf["id"])
        ],
    }
