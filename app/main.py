from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import get_db, init_db
from app.services import (
    add_dependency,
    abort_backfill,
    complete_backfill,
    create_backfill,
    describe_partition,
    execute_judgment,
    get_partition_detail,
    grace_disposition,
    disposition_label,
    ingest_backfill,
    list_backfill_events,
    list_backfills,
    list_dependencies,
    list_grace_registrations,
    list_judgments,
    list_partitions,
    list_policies,
    list_reports,
    list_runs,
    list_scan_jobs,
    register_grace,
    release_dependency,
    release_grace,
    run_expire_scan,
    run_reconcile,
    set_read_only,
)
from app.services.grace import GraceBlocked

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-retain-session")

app = FastAPI(title="cc-retain-lab")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("retain_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise HTTPException(status_code=401, detail="未登录")
    return user


async def _json_body(request: Request) -> dict:
    """空请求体按 {} 处理，避免无 body 的 POST 抛 JSON 解析错。"""
    raw = await request.body()
    if not raw or not raw.strip():
        return {}
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return body


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/")
def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return RedirectResponse("/app/", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/app/")
def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = await _json_body(request)
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return JSONResponse({"ok": False, "error": "账号或密码错误"}, status_code=400)
    resp = JSONResponse({"ok": True, "user": user})
    resp.set_cookie(
        "retain_session",
        serializer.dumps({"user": user}),
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("retain_session")
    return resp


@app.get("/api/meta")
def api_meta(request: Request):
    require_user(request)
    return {
        "lab_name": Config.LAB_NAME,
        "lab_code": Config.LAB_CODE,
        "skeleton": "分区留存策略版本→到期扫描裁决→归档或销毁执行→策略与实物分区对账",
        "grace_disposition": grace_disposition(),
        "grace_disposition_label": disposition_label(),
    }


@app.get("/api/policies")
def api_policies(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_policies(conn, active_only=True)}


@app.get("/api/partitions")
def api_partitions(request: Request, domain_code: str | None = None):
    require_user(request)
    with get_db() as conn:
        items = list_partitions(conn, domain_code=domain_code)
        for item in items:
            assessment = describe_partition(conn, item, action="销毁")
            item["grace"] = assessment
            item["grace_until"] = assessment["grace_until"]
            item["in_grace"] = assessment["in_grace"]
            item["days_left"] = assessment["days_left"]
            item["read_only"] = assessment["read_only"]
            item["frozen"] = assessment["frozen"]
            item["freeze_until"] = assessment["freeze_until"]
            item["can_destroy"] = assessment["can_destroy"]
            item["destroy_reasons"] = assessment["destroy_reasons"]
        return {"items": items}


@app.get("/api/partitions/{partition_id}")
def api_partition_detail(request: Request, partition_id: int):
    require_user(request)
    with get_db() as conn:
        detail = get_partition_detail(conn, partition_id)
        if not detail:
            raise HTTPException(status_code=404, detail="分区不存在")
        detail["grace_registrations"] = list_grace_registrations(conn, partition_id)
        return detail


@app.post("/api/partitions/{partition_id}/grace")
async def api_register_grace(request: Request, partition_id: int):
    user = require_user(request)
    body = await _json_body(request)
    until = str(body.get("until", "")).strip()
    if not until:
        raise HTTPException(status_code=400, detail="缺少 until")
    try:
        with get_db() as conn:
            result = register_grace(
                conn,
                partition_id,
                until,
                operator=user,
                reason=str(body.get("reason", "")),
                expected_rev=body.get("expected_rev"),
                mode=str(body.get("mode", "extend")),
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.delete("/api/partitions/{partition_id}/grace")
def api_release_grace(request: Request, partition_id: int):
    user = require_user(request)
    try:
        with get_db() as conn:
            result = release_grace(conn, partition_id, operator=user, reason="人工解除")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.post("/api/partitions/{partition_id}/read-only")
async def api_read_only(request: Request, partition_id: int):
    require_user(request)
    body = await _json_body(request)
    try:
        with get_db() as conn:
            result = set_read_only(
                conn,
                partition_id,
                bool(body.get("read_only")),
                reason=str(body.get("reason", "")),
                until=body.get("until") or None,
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.get("/api/dependencies")
def api_dependencies(request: Request, partition_id: int | None = None):
    require_user(request)
    with get_db() as conn:
        return {"items": list_dependencies(conn, partition_id)}


@app.post("/api/partitions/{partition_id}/dependencies")
async def api_add_dependency(request: Request, partition_id: int):
    require_user(request)
    body = await _json_body(request)
    try:
        with get_db() as conn:
            dep = add_dependency(
                conn,
                partition_id,
                dep_type=str(body.get("dep_type", "")),
                dep_ref=str(body.get("dep_ref", "")),
                blocking=bool(body.get("blocking", True)),
                note=str(body.get("note", "")),
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "dependency": dep}


@app.post("/api/dependencies/{dep_id}/release")
async def api_release_dependency(request: Request, dep_id: int):
    require_user(request)
    body = await _json_body(request)
    try:
        with get_db() as conn:
            dep = release_dependency(conn, dep_id, note=str(body.get("note", "")))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "dependency": dep}


@app.get("/api/backfills")
def api_backfills(request: Request, partition_id: int | None = None):
    require_user(request)
    with get_db() as conn:
        return {"items": list_backfills(conn, partition_id)}


@app.get("/api/backfill-events")
def api_backfill_events(request: Request, job_id: int | None = None):
    require_user(request)
    with get_db() as conn:
        return {"items": list_backfill_events(conn, job_id)}


@app.post("/api/partitions/{partition_id}/backfills")
async def api_create_backfill(request: Request, partition_id: int):
    user = require_user(request)
    body = await _json_body(request)
    try:
        with get_db() as conn:
            job = create_backfill(
                conn,
                partition_id,
                planned_rows=body.get("planned_rows"),
                grace_days=body.get("grace_days"),
                operator=user,
                note=str(body.get("note", "")),
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "backfill": job}


@app.post("/api/backfills/{job_id}/ingest")
async def api_ingest_backfill(request: Request, job_id: int):
    user = require_user(request)
    body = await _json_body(request)
    if "ingested_rows" not in body:
        raise HTTPException(status_code=400, detail="缺少 ingested_rows")
    try:
        with get_db() as conn:
            job = ingest_backfill(
                conn, job_id, ingested_rows=int(body["ingested_rows"]), operator=user
            )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "backfill": job}


@app.post("/api/backfills/{job_id}/complete")
async def api_complete_backfill(request: Request, job_id: int):
    user = require_user(request)
    body = await _json_body(request)
    try:
        with get_db() as conn:
            job = complete_backfill(
                conn, job_id, operator=user, note=str(body.get("note", ""))
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "backfill": job}


@app.post("/api/backfills/{job_id}/abort")
async def api_abort_backfill(request: Request, job_id: int):
    user = require_user(request)
    body = await _json_body(request)
    try:
        with get_db() as conn:
            job = abort_backfill(
                conn, job_id, operator=user, note=str(body.get("note", ""))
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "backfill": job}


@app.get("/api/scan-jobs")
def api_scan_jobs(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_scan_jobs(conn)}


@app.post("/api/scan-jobs")
async def api_create_scan(request: Request):
    require_user(request)
    body = await _json_body(request)
    as_of = str(body.get("as_of_date", "")).strip()
    if not as_of:
        raise HTTPException(status_code=400, detail="缺少 as_of_date")
    with get_db() as conn:
        job = run_expire_scan(conn, as_of)
    return {"ok": True, "job": job}


@app.get("/api/judgments")
def api_judgments(request: Request, status: str | None = None):
    require_user(request)
    with get_db() as conn:
        return {"items": list_judgments(conn, status=status)}


@app.post("/api/judgments/{judgment_id}/execute")
def api_execute(request: Request, judgment_id: int):
    require_user(request)
    try:
        with get_db() as conn:
            run = execute_judgment(conn, judgment_id)
    except GraceBlocked as exc:
        grace = exc.grace
        return JSONResponse(
            {
                "ok": False,
                "error": exc.message,
                "detail": exc.message,
                "disposition": exc.disposition,
                "disposition_label": disposition_label(exc.disposition),
                "partition_id": grace.get("partition_id"),
                "in_grace": grace.get("in_grace"),
                "grace_until": grace.get("grace_until"),
                "days_left": grace.get("days_left"),
                "can_destroy": grace.get("can_destroy"),
                "block_reasons": grace.get("block_reasons", []),
            },
            status_code=409,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "run": run}


@app.get("/api/runs")
def api_runs(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_runs(conn)}


@app.get("/api/reconcile-reports")
def api_reports(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_reports(conn)}


@app.post("/api/reconcile-reports")
async def api_reconcile(request: Request):
    require_user(request)
    body = await _json_body(request)
    as_of = str(body.get("as_of_date", "")).strip()
    if not as_of:
        raise HTTPException(status_code=400, detail="缺少 as_of_date")
    with get_db() as conn:
        report = run_reconcile(conn, as_of)
    return {"ok": True, "report": report}
