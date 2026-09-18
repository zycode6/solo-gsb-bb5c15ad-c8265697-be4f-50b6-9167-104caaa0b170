"""FastAPI 入口：导入、假设调整、重算、筛选、导出。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine
from .db import db, init_db
from .importer import import_payload, reset_all

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="事件证据链审计台",
    description="导入多来源 JSON 事件记录，校正时钟偏差，重建可解释时间线并定位冲突链。",
    version="1.0.0",
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ----------------------------------------------------------------- 模型

class SourceAssumption(BaseModel):
    clock_offset: float | None = None
    drift_ppm: float | None = None
    uncertainty: float | None = Field(default=None, ge=0)
    reliability: float | None = Field(default=None, ge=0, le=1)


class RecomputeBody(BaseModel):
    overrides: dict[str, SourceAssumption] = Field(default_factory=dict)
    source_id: str | None = None
    event_type: str | None = None
    q: str | None = None
    only_conflicts: bool = False
    only_ambiguous: bool = False
    lo: float | None = None
    hi: float | None = None


class SaveAssumptionsBody(BaseModel):
    assumptions: dict[str, SourceAssumption]


class ConstraintBody(BaseModel):
    from_event: str
    to_event: str
    kind: str = "claimed"
    note: str = ""


# ----------------------------------------------------------------- 路由

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/sample")
def download_sample() -> JSONResponse:
    from .sample import SAMPLE_PAYLOAD
    return JSONResponse(
        SAMPLE_PAYLOAD,
        headers={"Content-Disposition": 'attachment; filename="login-anomaly.json"'},
    )


@app.post("/api/import")
async def import_events(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(413, "文件过大（上限 8MB）")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"JSON 解析失败: {exc}") from exc
    try:
        summary = import_payload(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"imported": True, "summary": summary}


@app.post("/api/import/raw")
def import_raw(payload: Any = Body(...)) -> dict[str, Any]:
    """便于 curl/测试直接提交 JSON 体。"""
    try:
        return {"imported": True, "summary": import_payload(payload)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/recompute")
def recompute(body: RecomputeBody) -> dict[str, Any]:
    overrides = {sid: v.model_dump(exclude_none=True) for sid, v in body.overrides.items()}
    result = engine.compute(overrides)

    # 筛选只影响时间线展示；冲突/歧义始终全量计算，避免漏报。
    conflict_ids: set[str] = set()
    for c in result["conflicts"]:
        conflict_ids.update(c["events"])

    visible = _apply_filters(result["events"], body, conflict_ids)
    result["events"] = visible
    result["visible_count"] = len(visible)

    # 边也按可见事件裁剪
    visible_ids = {e["id"] for e in visible}
    if (body.source_id or body.event_type or body.q or body.lo is not None
            or body.hi is not None):
        result["edges"] = [e for e in result["edges"]
                           if e["from"] in visible_ids and e["to"] in visible_ids]
    result["overrides_applied"] = overrides
    return result


def _apply_filters(events: list[dict], body: RecomputeBody,
                   conflict_ids: set[str] | None = None) -> list[dict]:
    out = events
    if body.source_id:
        out = [e for e in out if e["source_id"] == body.source_id]
    if body.event_type:
        out = [e for e in out if e["event_type"] == body.event_type]
    if body.q:
        needle = body.q.lower()
        out = [e for e in out if needle in (e["summary"] or "").lower()
               or needle in e["id"].lower()
               or needle in (e.get("actor") or "").lower()
               or needle in e["event_type"].lower()]
    if body.lo is not None:
        out = [e for e in out if e["hi"] >= body.lo]
    if body.hi is not None:
        out = [e for e in out if e["lo"] <= body.hi]
    if body.only_conflicts:
        out = [e for e in out if e["id"] in (conflict_ids or set())]
    if body.only_ambiguous:
        out = [e for e in out if e.get("ambiguity_group")]
    return out


@app.post("/api/sources/assumptions")
def save_assumptions(body: SaveAssumptionsBody) -> dict[str, Any]:
    """把审计员确认后的偏差假设持久化。"""
    updated: list[str] = []
    with db() as conn:
        for sid, a in body.assumptions.items():
            row = conn.execute("SELECT id FROM sources WHERE id=?", (sid,)).fetchone()
            if row is None:
                continue
            data = a.model_dump(exclude_none=True)
            sets, vals = [], []
            for k, v in data.items():
                sets.append(f"{k}=?")
                vals.append(float(v))
            if not sets:
                continue
            sets.append("updated_at=CURRENT_TIMESTAMP")
            vals.append(sid)
            conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?", vals)
            updated.append(sid)
    return {"saved": True, "updated_sources": updated}


@app.get("/api/sources")
def list_sources() -> dict[str, Any]:
    result = engine.compute()
    return {"sources": result["sources"]}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str) -> dict[str, Any]:
    with db() as conn:
        cur = conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "来源不存在")
    return {"deleted": source_id}


@app.post("/api/constraints")
def add_constraint(body: ConstraintBody) -> dict[str, Any]:
    if body.from_event == body.to_event:
        raise HTTPException(400, "自环约束无意义")
    with db() as conn:
        for eid in (body.from_event, body.to_event):
            if conn.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone() is None:
                raise HTTPException(404, f"事件不存在: {eid}")
        cur = conn.execute(
            "INSERT OR IGNORE INTO constraints(from_event,to_event,kind,note,created_at) "
            "VALUES(?,?,?,?,CURRENT_TIMESTAMP)",
            (body.from_event, body.to_event, body.kind, body.note),
        )
        if cur.rowcount == 0:
            raise HTTPException(409, "约束已存在")
    return {"created": True}


@app.delete("/api/constraints/{edge_id}")
def delete_constraint(edge_id: int) -> dict[str, Any]:
    with db() as conn:
        cur = conn.execute("DELETE FROM constraints WHERE id=?", (edge_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "约束不存在")
    return {"deleted": edge_id}


@app.get("/api/event-types")
def event_types() -> dict[str, Any]:
    with db() as conn:
        rows = conn.execute(
            "SELECT event_type, COUNT(*) n FROM events GROUP BY event_type ORDER BY n DESC"
        ).fetchall()
    return {"types": [{"name": r["event_type"], "count": r["n"]} for r in rows]}


@app.post("/api/reset")
def reset_data() -> dict[str, Any]:
    reset_all()
    return {"reset": True}


@app.get("/api/export")
def export_audit(clock_offset: str | None = None, drift_ppm: str | None = None,
                 uncertainty: str | None = None) -> JSONResponse:
    """导出完整审计结果 JSON；可经 query 传一次性假设（用于未保存即导出）。"""
    overrides: dict[str, dict[str, float]] = {}
    for name, raw in (("clock_offset", clock_offset), ("drift_ppm", drift_ppm),
                      ("uncertainty", uncertainty)):
        if not raw:
            continue
        for item in raw.split(","):
            if ":" not in item:
                continue
            sid, val = item.split(":", 1)
            try:
                overrides.setdefault(sid.strip(), {})[name] = float(val)
            except ValueError:
                raise HTTPException(400, f"无法解析 {name}: {item}")
    payload = engine.build_export(overrides)
    return JSONResponse(
        payload,
        headers={"Content-Disposition": 'attachment; filename="audit_result.json"'},
    )


# ----------------------------------------------------------------- 前端

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
