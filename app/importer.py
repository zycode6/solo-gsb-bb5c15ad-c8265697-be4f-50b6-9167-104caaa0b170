"""多来源 JSON 事件记录导入器。

支持三种载荷形态
----------------
1. 整包对象::

       {
         "sources": [{"name": "srv-a", "clock_offset_seconds": 5.0, ...}],
         "events":  [{"id": "e1", "source": "srv-a", "timestamp": "...", ...}]
       }

2. 顶层事件数组 ``[ {...}, {...} ]``
3. NDJSON：每行一个事件对象（部分坏行会被跳过并记录为 warning）

事件字段做了宽松映射，例如 timestamp / time / ts / @timestamp 均可；
before/after/predecessor/precedes 用来重建显式因果链。

导入模式
--------
- replace（默认）：清空当前工作区后导入；
- merge：按 (来源名, 外部ID) 去重，已存在的先删除再写入。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .database import reset_workspace, session
from .engine import recompute

EVENT_ID_KEYS = ("id", "event_id", "eid", "uuid", "record_id")
SOURCE_KEYS = ("source", "source_name", "src", "host", "origin", "node", "device")
TS_KEYS = ("timestamp", "time", "ts", "event_time", "@timestamp", "occurred_at", "logged_at")
TYPE_KEYS = ("type", "event_type", "kind", "action")
SUMMARY_KEYS = ("summary", "message", "msg", "title", "description", "detail")
BEFORE_KEYS = ("before", "next", "successor", "successors", "leads_to", "then")
AFTER_KEYS = ("after", "prev", "predecessor", "predecessors", "follows", "caused_by", "parent")

SOURCE_PARAM_MAP = {
    "offset": "offset_seconds",
    "offset_seconds": "offset_seconds",
    "clock_offset_seconds": "offset_seconds",
    "drift": "drift_ppm",
    "drift_ppm": "drift_ppm",
    "uncertainty": "uncertainty_seconds",
    "uncertainty_seconds": "uncertainty_seconds",
    "jitter": "uncertainty_seconds",
    "trust": "trust",
    "confidence": "trust",
    "monotonic": "monotonic",
    "ordered": "monotonic",
    "note": "note",
    "description": "note",
}
SOURCE_NAME_KEYS = ("name", "id", "source", "source_name")


class ImportError_(Exception):
    pass


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def parse_timestamp(value: Any) -> float:
    """把各种时间表达转换为 epoch 秒（UTC）。"""
    if value is None:
        raise ValueError("缺少时间戳")
    if isinstance(value, (int, float)):
        v = float(value)
        # 秒 / 毫秒 / 微秒 / 纳秒级整数时间戳归一化到秒（互斥区间）
        a = abs(v)
        if a >= 1e17:        # ns
            v /= 1e9
        elif a >= 1e14:      # us
            v /= 1e6
        elif a >= 1e11:      # ms
            v /= 1e3
        return v
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError("空时间戳")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # 再试几种常见格式
            for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%b/%Y:%H:%M:%S %z"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"无法解析时间戳: {value!r}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValueError(f"不支持的时间戳类型: {type(value).__name__}")


def _first_present(d: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _as_id_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x is not None]
    return [str(v)]


def _coerce_float(v: Any, field: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"参数 {field} 需要数值，收到 {v!r}")


# --------------------------------------------------------------------------- #
# 载荷解析
# --------------------------------------------------------------------------- #
def parse_payload(text: str, filename: str = "") -> tuple[list[dict], list[dict], list[str]]:
    """返回 (source_defs, event_records, warnings)。"""
    warnings: list[str] = []
    text = text.strip()
    if not text:
        raise ImportError_("文件为空")

    records: list[dict] = []
    source_defs: list[dict] = []

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            if isinstance(data.get("events"), list):
                records = data["events"]
                raw_sources = data.get("sources", [])
                if isinstance(raw_sources, dict):  # {"srv-a": {...}} 形态
                    raw_sources = [{"name": k, **(v if isinstance(v, dict) else {})} for k, v in raw_sources.items()]
                source_defs = raw_sources if isinstance(raw_sources, list) else []
            elif isinstance(data.get("records"), list):
                records = data["records"]
            else:
                records = [data]  # 单条事件对象
        elif isinstance(data, list):
            records = data
        else:
            raise ImportError_("JSON 顶层必须是对象或数组")
    except json.JSONDecodeError as je:
        # 尝试 NDJSON
        nd_records: list[dict] = []
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"第 {i} 行不是合法 JSON，已跳过")
                continue
            if isinstance(obj, dict):
                nd_records.append(obj)
        if not nd_records:
            raise ImportError_(f"既不是合法 JSON 也不是 NDJSON：{je.msg} (第 {je.lineno} 行)")
        warnings.append("已按 NDJSON 逐行解析")
        records = nd_records

    # 内联来源定义：事件里的 source 也可以是带参数的对象
    for r in records:
        if not isinstance(r, dict):
            continue
        src = _first_present(r, SOURCE_KEYS)
        if isinstance(src, dict):
            inline = {"name": _first_present(src, SOURCE_NAME_KEYS), **{k: v for k, v in src.items() if k not in SOURCE_NAME_KEYS}}
            if inline["name"]:
                source_defs.append(inline)

    return source_defs, records, warnings


# --------------------------------------------------------------------------- #
# 入库
# --------------------------------------------------------------------------- #
def _upsert_source(conn, name: str, defs_by_name: dict[str, dict]) -> int:
    row = conn.execute("SELECT id FROM sources WHERE name=?", (name,)).fetchone()
    if row:
        sid = row["id"]
        d = defs_by_name.get(name)
        if d:
            _apply_source_params(conn, sid, d)
        return sid

    d = defs_by_name.get(name, {})
    conn.execute(
        "INSERT INTO sources (name, offset_seconds, drift_ppm, uncertainty_seconds, trust, monotonic, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            float(d.get("offset_seconds", 0.0)),
            float(d.get("drift_ppm", 0.0)),
            float(d.get("uncertainty_seconds", 2.0)),
            min(1.0, max(0.0, float(d.get("trust", 0.8)))),
            1 if _truthy(d.get("monotonic", True)) else 0,
            str(d.get("note", "")),
        ),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _apply_source_params(conn, sid: int, d: dict) -> None:
    cur = conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    params = dict(cur)
    for raw_k, v in d.items():
        col = SOURCE_PARAM_MAP.get(raw_k)
        if not col or v is None:
            continue
        if col in ("offset_seconds", "drift_ppm", "uncertainty_seconds"):
            params[col] = _coerce_float(v, raw_k)
        elif col == "trust":
            params[col] = min(1.0, max(0.0, _coerce_float(v, raw_k)))
        elif col == "monotonic":
            params[col] = 1 if _truthy(v) else 0
        else:
            params[col] = str(v)
    conn.execute(
        "UPDATE sources SET offset_seconds=?, drift_ppm=?, uncertainty_seconds=?, trust=?, monotonic=?, note=? WHERE id=?",
        (
            params["offset_seconds"], params["drift_ppm"], params["uncertainty_seconds"],
            params["trust"], params["monotonic"], params["note"], sid,
        ),
    )


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(v)


def import_text(text: str, mode: str = "replace", filename: str = "",
                db_path: str | None = None) -> dict[str, Any]:
    source_defs, records, warnings = parse_payload(text, filename)

    # 归一化来源定义
    defs_by_name: dict[str, dict] = {}
    for sd in source_defs:
        if not isinstance(sd, dict):
            warnings.append(f"来源定义不是对象，已跳过: {sd!r}")
            continue
        name = _first_present(sd, SOURCE_NAME_KEYS)
        if not name:
            warnings.append("来源定义缺少 name/id，已跳过")
            continue
        name = str(name)
        norm: dict[str, Any] = {}
        for k, v in sd.items():
            col = SOURCE_PARAM_MAP.get(k)
            if col and v is not None:
                norm[col] = v
        # 已存在的同名定义合并，后者覆盖
        defs_by_name.setdefault(name, {}).update(norm)

    with session(db_path) as conn:
        if mode == "replace":
            reset_workspace(conn)
        elif mode != "merge":
            raise ImportError_(f"未知导入模式: {mode}")

        # merge 模式下重复事件去重：按 (source_name, ext_id) 找旧事件
        source_cache: dict[str, int] = {}

        def get_sid(name: str) -> int:
            if name not in source_cache:
                source_cache[name] = _upsert_source(conn, name, defs_by_name)
            return source_cache[name]

        imported = 0
        skipped = 0
        seq_counter: dict[int, int] = {}
        # ext_id -> 新 event db id（本次导入内解析链接用）
        ext_to_db: dict[str, int] = {}
        if mode == "merge":
            for r in conn.execute(
                "SELECT e.id, e.ext_id, s.name AS sname FROM events e JOIN sources s ON s.id=e.source_id"
            ).fetchall():
                if r["ext_id"]:
                    ext_to_db[f'{r["sname"]}::{r["ext_id"]}'] = r["id"]

        pending_links: list[tuple[str, str, str]] = []  # (from_key, to_key, origin_hint)

        for idx, r in enumerate(records):
            if not isinstance(r, dict):
                warnings.append(f"第 {idx + 1} 条记录不是对象，已跳过")
                skipped += 1
                continue
            try:
                raw_ts = parse_timestamp(_first_present(r, TS_KEYS))
            except ValueError as ex:
                warnings.append(f"第 {idx + 1} 条记录{('(' + str(r.get('id', '?')) + ')') if r.get('id') else ''}: {ex}，已跳过")
                skipped += 1
                continue

            src_name = _first_present(r, SOURCE_KEYS)
            if isinstance(src_name, dict):
                src_name = _first_present(src_name, SOURCE_NAME_KEYS)
            if not src_name:
                src_name = "unknown"
            src_name = str(src_name)

            ext_id = _first_present(r, EVENT_ID_KEYS)
            ext_id = str(ext_id) if ext_id is not None else ""
            sid = get_sid(src_name)

            if mode == "merge" and ext_id:
                key = f"{src_name}::{ext_id}"
                old = ext_to_db.get(key)
                if old:
                    conn.execute("DELETE FROM events WHERE id=?", (old,))

            seq_counter[sid] = seq_counter.get(sid, 0) + 1
            jitter_v = r.get("jitter_seconds", r.get("jitter", 0.0))
            try:
                jitter = max(0.0, float(jitter_v)) if jitter_v is not None else 0.0
            except (TypeError, ValueError):
                jitter = 0.0

            event_type = str(_first_present(r, TYPE_KEYS, ""))
            summary = str(_first_present(r, SUMMARY_KEYS, ""))
            raw_json = json.dumps(r, ensure_ascii=False, sort_keys=True)

            conn.execute(
                "INSERT INTO events (ext_id, source_id, seq, event_type, summary, raw_ts, jitter, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ext_id, sid, seq_counter[sid], event_type, summary, raw_ts, jitter, raw_json),
            )
            db_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            if ext_id:
                ext_to_db[f"{src_name}::{ext_id}"] = db_id
            imported += 1

            # 显式链接：记录外部 key，全部入库后解析（支持前向引用）
            for target in _as_id_list(_first_present(r, BEFORE_KEYS)):
                pending_links.append((f"{src_name}::{ext_id}" if ext_id else str(db_id),
                                      _qualify_target(target, defs_by_name, src_name), "before"))
            for target in _as_id_list(_first_present(r, AFTER_KEYS)):
                pending_links.append((_qualify_target(target, defs_by_name, src_name),
                                      f"{src_name}::{ext_id}" if ext_id else str(db_id), "after"))

        conn.commit()

        # 解析显式链接
        dangling: dict[str, int] = {}
        # 未限定来源的 ext_id：在全部事件中做唯一匹配
        plain_index: dict[str, list[int]] = {}
        for r in conn.execute(
            "SELECT e.id, e.ext_id FROM events e WHERE e.ext_id != ''"
        ).fetchall():
            plain_index.setdefault(r["ext_id"], []).append(r["id"])

        def resolve(key: str) -> int | None:
            if "::" in key:
                if key in ext_to_db:
                    return ext_to_db[key]
                bare = key.split("::", 1)[1]
            else:
                bare = key
            hits = plain_index.get(bare, [])
            # 裸 ID 命中多条（跨来源重名）时不做猜测，按悬空处理
            return hits[0] if len(hits) == 1 else None

        for from_key, to_key, _hint in pending_links:
            a, b = resolve(from_key), resolve(to_key)
            if not a or not b:
                dangling[f"{from_key} -> {to_key}"] = dangling.get(f"{from_key} -> {to_key}", 0) + 1
                continue
            if a == b:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO links (from_event, to_event, kind) VALUES (?, ?, 'explicit')",
                (a, b),
            )
        for link_desc, cnt in dangling.items():
            warnings.append(f"悬空因果链（目标事件未找到，忽略 x{cnt}）：{link_desc}")

        stats = recompute(conn)

    stats.update({"imported": imported, "skipped": skipped, "warnings": warnings, "mode": mode})
    return stats


def _qualify_target(target: str, defs_by_name: dict[str, dict], current_source: str) -> str:
    """``srv-b/e7`` 形式限定来源；否则先尝试 source:id 否则视为同源裸 ID。"""
    target = target.strip()
    if "/" in target:
        s, eid = target.split("/", 1)
        return f"{s}::{eid}"
    if ":" in target:
        s, eid = target.split(":", 1)
        return f"{s}::{eid}"
    return f"{current_source}::{target}"
