import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发写（两人抢推宽限/裁决）时排队等待，而不是直接 database is locked。
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def immediate_txn(conn: sqlite3.Connection):
    """排他写事务：BEGIN IMMEDIATE 先拿写锁，再做读-改-写。

    宽限截止日、补采进度、裁决执行都必须走这里，否则两个并发请求会各自
    读到旧值再分别写回，造成截止日互相覆盖（lost update）。
    """
    if getattr(conn, "in_transaction", False):
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS retain_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_code TEXT NOT NULL,
  domain_name TEXT NOT NULL,
  keep_days INTEGER NOT NULL,
  action_on_expire TEXT NOT NULL CHECK (action_on_expire IN ('归档','销毁')),
  version INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_code, version)
);

CREATE TABLE IF NOT EXISTS partitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  row_count INTEGER NOT NULL DEFAULT 0,
  storage_state TEXT NOT NULL CHECK (
    storage_state IN ('在线','已归档','已销毁','裁撤中')
  ),
  archive_uri TEXT,
  grace_until TEXT,
  manual_grace_until TEXT,
  read_only INTEGER NOT NULL DEFAULT 0,
  freeze_until TEXT,
  grace_rev INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_code, biz_date)
);

CREATE TABLE IF NOT EXISTS scan_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  status TEXT NOT NULL,
  expired_count INTEGER NOT NULL DEFAULT 0,
  grace_blocked_count INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS judgments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_job_id INTEGER NOT NULL,
  partition_id INTEGER NOT NULL,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  policy_version INTEGER NOT NULL,
  keep_days INTEGER NOT NULL,
  decided_action TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待执行','执行中','已完成','已失败','已作废')),
  fail_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(scan_job_id) REFERENCES scan_jobs(id),
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);

CREATE TABLE IF NOT EXISTS execute_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  judgment_id INTEGER NOT NULL UNIQUE,
  action TEXT NOT NULL,
  result_state TEXT NOT NULL,
  archive_uri TEXT,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(judgment_id) REFERENCES judgments(id)
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  online_count INTEGER NOT NULL,
  archived_count INTEGER NOT NULL,
  destroyed_count INTEGER NOT NULL,
  pending_judgment_count INTEGER NOT NULL,
  mismatch_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 补采编排：进行中的补采会在分区登记宽限，入库未完成前禁止销毁。
CREATE TABLE IF NOT EXISTS backfill_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('未开始','进行中','已完成','已中止')),
  planned_rows INTEGER NOT NULL DEFAULT 0,
  ingested_rows INTEGER NOT NULL DEFAULT 0,
  grace_days INTEGER NOT NULL DEFAULT 3,
  grace_until TEXT,
  operator TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);

-- 依赖列表：生效且 blocking 的依赖同样冻结销毁。
CREATE TABLE IF NOT EXISTS partition_dependencies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  dep_type TEXT NOT NULL CHECK (dep_type IN ('补采','下游消费','跨域引用')),
  dep_ref TEXT NOT NULL,
  blocking INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('生效','已解除')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  released_at TEXT,
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);

-- 宽限登记流水：每次登记/解除都留痕，便于本机库核对并发写入结果。
CREATE TABLE IF NOT EXISTS grace_registrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('登记','解除','重算')),
  requested_until TEXT NOT NULL DEFAULT '',
  effective_until TEXT NOT NULL DEFAULT '',
  operator TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 补采编排流水：发起/上报/完成/中止都留痕，便于本机库核对补采进度与宽限联动。
CREATE TABLE IF NOT EXISTS backfill_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backfill_id INTEGER NOT NULL,
  partition_id INTEGER NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('发起','上报','完成','中止')),
  ingested_rows INTEGER NOT NULL DEFAULT 0,
  planned_rows INTEGER NOT NULL DEFAULT 0,
  grace_until TEXT,
  operator TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(backfill_id) REFERENCES backfill_jobs(id),
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);

CREATE INDEX IF NOT EXISTS idx_judgments_partition ON judgments(partition_id);
CREATE INDEX IF NOT EXISTS idx_backfill_partition ON backfill_jobs(partition_id);
CREATE INDEX IF NOT EXISTS idx_backfill_events ON backfill_events(backfill_id);
CREATE INDEX IF NOT EXISTS idx_deps_partition ON partition_dependencies(partition_id);
CREATE INDEX IF NOT EXISTS idx_grace_partition ON grace_registrations(partition_id);
"""


SEED_POLICIES = [
    ("orders", "订单明细域", 7, "归档"),
    ("payments", "支付流水域", 14, "归档"),
    ("events", "埋点事件域", 3, "销毁"),
    ("inventory", "库存快照域", 10, "归档"),
]

SEED_PARTITIONS = [
    ("orders", "2026-11-01", 1200, "在线"),
    ("orders", "2026-11-05", 980, "在线"),
    ("orders", "2026-11-10", 1100, "在线"),
    ("orders", "2026-11-14", 1050, "在线"),
    ("payments", "2026-10-20", 800, "在线"),
    ("payments", "2026-11-01", 760, "在线"),
    ("payments", "2026-11-12", 790, "在线"),
    ("events", "2026-11-08", 5000, "在线"),
    ("events", "2026-11-12", 5200, "在线"),
    ("events", "2026-11-15", 5100, "在线"),
    ("inventory", "2026-10-30", 300, "在线"),
    ("inventory", "2026-11-11", 310, "在线"),
]


def _reference_date() -> date:
    raw = os.environ.get("RETAIN_REFERENCE_DATE", "").strip()
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    return date.today()


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        ensure_grace_columns(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM retain_policies").fetchone()["c"]
        if n == 0:
            for domain_code, domain_name, keep_days, action in SEED_POLICIES:
                conn.execute(
                    "INSERT INTO retain_policies "
                    "(domain_code, domain_name, keep_days, action_on_expire, version, active) "
                    "VALUES (?,?,?,?,1,1)",
                    (domain_code, domain_name, keep_days, action),
                )
            for domain_code, biz_date, row_count, state in SEED_PARTITIONS:
                conn.execute(
                    "INSERT INTO partitions "
                    "(domain_code, biz_date, row_count, storage_state) "
                    "VALUES (?,?,?,?)",
                    (domain_code, biz_date, row_count, state),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('lab_name', ?)",
                (Config.LAB_NAME,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('skeleton', ?)",
                (
                    "分区留存策略版本→到期扫描裁决→归档或销毁执行→策略与实物分区对账",
                ),
            )
        seed_governance(conn)


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def ensure_grace_columns(conn):
    """对已存在的 instance 库做增量补列（幂等）。"""
    _ensure_columns(
        conn,
        "partitions",
        {
            "grace_until": "TEXT",
            "manual_grace_until": "TEXT",
            "read_only": "INTEGER NOT NULL DEFAULT 0",
            "freeze_until": "TEXT",
            "grace_rev": "INTEGER NOT NULL DEFAULT 0",
        },
    )
    _ensure_columns(
        conn,
        "scan_jobs",
        {"grace_blocked_count": "INTEGER NOT NULL DEFAULT 0"},
    )


def seed_governance(conn) -> None:
    """演示数据：一个仍在宽限期的销毁分区 + 未完成补采 + 依赖列表。

    只在补采表为空时写入，兼容已有 instance 库的增量初始化。
    """
    if conn.execute("SELECT COUNT(*) AS c FROM backfill_jobs").fetchone()["c"]:
        return
    with immediate_txn(conn):
        if conn.execute("SELECT COUNT(*) AS c FROM backfill_jobs").fetchone()["c"]:
            return
        row = conn.execute(
            "SELECT * FROM partitions WHERE domain_code = 'events' "
            "AND biz_date = '2026-11-12'"
        ).fetchone()
        if not row:
            return
        grace_until = (_reference_date() + timedelta(days=3)).isoformat()
        planned = int(row["row_count"])
        cur = conn.execute(
            "INSERT INTO backfill_jobs "
            "(partition_id, domain_code, biz_date, status, planned_rows, "
            "ingested_rows, grace_days, grace_until, operator, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["domain_code"],
                row["biz_date"],
                "进行中",
                planned,
                int(planned * 0.6),
                3,
                grace_until,
                "seed",
                "补采入库未完成，宽限冻结销毁",
            ),
        )
        job_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO partition_dependencies "
            "(partition_id, dep_type, dep_ref, blocking, status, note) "
            "VALUES (?,?,?,1,'生效',?)",
            (row["id"], "补采", f"backfill#{job_id}", "补采入库未完成，禁止销毁"),
        )
        conn.execute(
            "INSERT INTO backfill_events "
            "(backfill_id, partition_id, action, ingested_rows, planned_rows, "
            "grace_until, operator, note) VALUES (?,?,?,?,?,?,?,?)",
            (
                job_id,
                row["id"],
                "发起",
                int(planned * 0.6),
                planned,
                grace_until,
                "seed",
                "演示数据：补采进行中，宽限冻结销毁",
            ),
        )
        conn.execute(
            "INSERT INTO partition_dependencies "
            "(partition_id, dep_type, dep_ref, blocking, status, note) "
            "VALUES (?,?,?,1,'生效',?)",
            (row["id"], "下游消费", "ads_events_daily", "下游日报仍引用该分区"),
        )
        orders = conn.execute(
            "SELECT id FROM partitions WHERE domain_code = 'orders' "
            "AND biz_date = '2026-11-05'"
        ).fetchone()
        if orders:
            conn.execute(
                "INSERT INTO partition_dependencies "
                "(partition_id, dep_type, dep_ref, blocking, status, note, released_at) "
                "VALUES (?,?,?,0,'已解除',?,datetime('now'))",
                (orders["id"], "下游消费", "ads_orders_daily", "下游已切换完成，依赖解除"),
            )
        conn.execute(
            "UPDATE partitions SET grace_until = ? WHERE id = ?",
            (grace_until, row["id"]),
        )
        conn.execute(
            "INSERT INTO grace_registrations "
            "(partition_id, action, requested_until, effective_until, operator, reason) "
            "VALUES (?,?,?,?,?,?)",
            (
                row["id"],
                "登记",
                grace_until,
                grace_until,
                "seed",
                f"补采任务#{job_id}登记宽限",
            ),
        )
