"""FastAPI 主应用：审计台后端。

启动时自动建库；所有重算在 SQLite 默认路径上就地完成。
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import sample_data
from .database import connect, init_db, reset_workspace, session
from .engine import recompute
from .importer import ImportError_, import_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "web")


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_state(conn) -> dict[str, Any]:
    sources = [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY id")]
    events = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY ordinal, id")]
    constraints = [dict(r) for r in conn.execute("SELECT * FROM derived_constraints")]
    conflicts = [dict(r) for r in conn.execute("SELECT * FROM conflicts ORDER BY id")]
    links = [dict(r) for r in conn.execute("SELECT * FROM links")]

    source_by_id = {s["id"]: s for s in sources}

    conflict_events_by_source: dict[int, int] = {}
    for e in events:
        if e["conflict_flag"]:
            conflict_events_by_source[e["source_id"]] = conflict_events_by_source.get(e["source_id"], 0) + 1

    out_sources = []
    for s in sources:
        out_sources.append({
            "id": s["id"], "name": s["name"],
            "offset_seconds": s["offset_seconds"],
            "drift_ppm": s["drift_ppm"],
            "uncertainty_seconds": s["uncertainty_seconds"],
            "trust": s["trust"],
            "monotonic": bool(s["monotonic"]),
            "note": s["note"],
            "conflict_events": conflict_events_by_source.get(s["id"], 0),
        })

    out_events = []
    for e in events:
        s = source_by_id.get(e["source_id"])
        out_events.append({
            "id": e["id"],
            "ext_id": e["ext_id"],
            "source": s["name"] if s else f"#{e['source_id']}",
            "source_id": e["source_id"],
            "seq": e["seq"],
            "type": e["event_type"],
            "summary": e["summary"],
            "raw_ts": _iso(e["raw_ts"]),
            "raw_epoch": e["raw_ts"],
            "jitter": e["jitter"],
            "ref_mid": _iso(e["ref_mid"]),
            "ref_lower": _iso(e["ref_lower"]),
            "ref_upper": _iso(e["ref_upper"]),
            "ref_lower_epoch": e["ref_lower"],
            "ref_upper_epoch": e["ref_upper"],
            "confidence": e["confidence"],
            "ordinal": e["ordinal"],
            "in_conflict": bool(e["conflict_flag"]),
        })

    id_to_ext: dict[int, str] = {}
    for e in events:
        id_to_ext[e["id"]] = e["ext_id"] or f"#{e['id']}"

    out_constraints = []
    for c in constraints:
        fe, te = c["from_event"], c["to_event"]
        out_constraints.append({
            "from_event": fe, "to_event": te,
            "from_ext": id_to_ext.get(fe), "to_ext": id_to_ext.get(te),
            "origin": c["origin"], "status": c["status"],
        })

    out_links = [{"from_event": l["from_event"], "to_event": l["to_event"]} for l in links]

    out_conflicts = []
    for c in conflicts:
        try:
            chain_ids = json.loads(c["event_ids"])
        except json.JSONDecodeError:
            chain_ids = []
        out_conflicts.append({
            "id": c["id"], "kind": c["kind"],
            "chain": [id_to_ext.get(x, str(x)) for x in chain_ids],
            "chain_db_ids": chain_ids,
            "detail": c["detail"], "created_at": c["created_at"],
        })

    conflict_edges = [c for c in out_constraints if c["status"] == "impossible"]
    stats = {
        "event_count": len(events),
        "source_count": len(sources),
        "conflict_count": len(conflicts),
        "conflict_event_count": sum(1 for e in events if e["conflict_flag"]),
        "impossible_edge_count": len(conflict_edges),
        "ambiguous_edge_count": sum(1 for c in out_constraints if c["status"] == "ambiguous"),
        "forced_edge_count": sum(1 for c in out_constraints if c["status"] == "forced"),
        "avg_confidence": round(sum(e["confidence"] or 0 for e in events) / len(events), 3) if events else None,
        "span": {
            "start": _iso(min((e["ref_lower"] for e in events), default=None)),
            "end": _iso(max((e["ref_upper"] for e in events), default=None)),
        },
    }
    return {
        "stats": stats,
        "sources": out_sources,
        "events": out_events,
        "constraints": out_constraints,
        "links": out_links,
        "conflicts": out_conflicts,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class SourceUpdate(BaseModel):
    offset_seconds: float | None = None
    drift_ppm: float | None = None
    uncertainty_seconds: float | None = Field(default=None, ge=0)
    trust: float | None = Field(default=None, ge=0, le=1)
    monotonic: bool | None = None
    note: str | None = None


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    with session() as conn:
        init_db(conn)
    yield


app = FastAPI(title="事件证据链审计台", version="1.0.0", lifespan=lifespan)


@app.get("/api/state")
def get_state() -> dict[str, Any]:
    with session() as conn:
        return build_state(conn)


@app.post("/api/recompute")
def post_recompute() -> dict[str, Any]:
    with session() as conn:
        stats = recompute(conn)
        return build_state(conn) | {"recompute": stats}


@app.post("/api/import")
async def post_import(
    file: UploadFile | None = File(default=None),
    mode: str = Form(default="replace"),
    payload: str | None = Form(default=None),
) -> dict[str, Any]:
    if mode not in ("replace", "merge"):
        raise HTTPException(400, "mode 只能是 replace 或 merge")
    if file is not None:
        raw = await file.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        filename = file.filename or ""
    elif payload is not None:
        text, filename = payload, "inline.json"
    else:
        raise HTTPException(400, "请提供 file 文件或 payload 文本")
    try:
        import_stats = import_text(text, mode=mode, filename=filename)
    except ImportError_ as ex:
        raise HTTPException(400, str(ex))
    with session() as conn:
        state = build_state(conn)
    state["import"] = import_stats
    return state


@app.post("/api/sample")
def post_sample(mode: str = "replace") -> dict[str, Any]:
    if mode not in ("replace", "merge"):
        raise HTTPException(400, "mode 只能是 replace 或 merge")
    import_stats = import_text(sample_data.payload(), mode=mode, filename="sample.json")
    with session() as conn:
        state = build_state(conn)
    state["import"] = import_stats
    return state


@app.patch("/api/sources/{source_id}")
def patch_source(source_id: int, update: SourceUpdate) -> dict[str, Any]:
    fields = update.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "没有需要更新的字段")
    with session() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(404, "来源不存在")
        current = dict(row)
        current.update(fields)
        conn.execute(
            "UPDATE sources SET offset_seconds=?, drift_ppm=?, uncertainty_seconds=?, trust=?, monotonic=?, note=? WHERE id=?",
            (
                current["offset_seconds"], current["drift_ppm"], current["uncertainty_seconds"],
                current["trust"], 1 if current["monotonic"] else 0, current["note"], source_id,
            ),
        )
        recompute(conn)
        return build_state(conn)


@app.post("/api/reset")
def post_reset() -> dict[str, Any]:
    with session() as conn:
        reset_workspace(conn)
        return build_state(conn)


@app.get("/api/export")
def export_audit() -> Response:
    """导出完整审计结果 JSON（含当前偏差假设、派生区间、冲突链与原始记录引用）。"""
    with session() as conn:
        state = build_state(conn)
        raws = [
            {
                "db_id": r["id"], "ext_id": r["ext_id"],
                "source_id": r["source_id"], "raw_ts_epoch": r["raw_ts"],
                "raw": json.loads(r["raw_json"] or "{}"),
            }
            for r in conn.execute("SELECT * FROM events ORDER BY id")
        ]

    state["audit_version"] = "1.0"
    state["raw_records"] = raws
    state["model"] = {
        "time_correction": "ref(t) = t + offset + drift_ppm*1e-6*(t - t_earliest)",
        "interval_half_width": "jitter + source.uncertainty_seconds",
        "confidence": "0.65*source.trust + 0.25*interval_sharpness + 0.10*consistency",
        "constraints": ["explicit before/after from JSON", "same-source monotonic clock assumption"],
    }
    fname = f"audit-result-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    body = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=body, media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Conflict-Count": str(state["stats"]["conflict_count"]),
        },
    )


# 未知 /api 路径返回 JSON 404（须在静态文件挂载之前注册）
@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
def api_not_found(rest: str) -> JSONResponse:
    return JSONResponse({"detail": f"接口不存在: /api/{rest}"}, status_code=404)


# 静态前端必须最后挂载，避免吞掉 /api 路由
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="web")
