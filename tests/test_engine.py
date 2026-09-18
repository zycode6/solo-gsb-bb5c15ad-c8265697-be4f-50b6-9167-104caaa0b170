"""引擎与导入器的核心单元测试：

运行：python -m pytest -q
（不依赖 pytest 时也可以直接执行：python tests/test_engine.py）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import sample_data
from app.database import connect, init_db, session
from app.engine import recompute
from app.importer import import_text, parse_timestamp


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = connect(path)
    init_db(conn)
    conn.close()
    return path


def test_parse_timestamp_units():
    assert abs(parse_timestamp(0) - 0) < 1e-6
    assert abs(parse_timestamp(1_700_000_000_000) - 1_700_000_000) < 1e-6
    assert abs(parse_timestamp(1_700_000_000_000_000) - 1_700_000_000) < 1e-3
    assert abs(parse_timestamp("2023-11-14T22:13:20Z") - 1_700_000_000) < 1
    assert abs(parse_timestamp("2023-11-14T22:13:20+00:00") - 1_700_000_000) < 1


def test_sample_has_conflicts_before_skew_fix():
    path = fresh_db()
    try:
        stats = import_text(sample_data.payload(), mode="replace", db_path=path)
        assert stats["imported"] == 9
        assert stats["conflict_count"] >= 2, stats
        kinds = set()
        with session(path) as conn:
            for row in conn.execute("SELECT kind FROM conflicts"):
                kinds.add(row["kind"])
        assert "interval" in kinds and "cycle" in kinds
    finally:
        os.unlink(path)


def test_interval_conflict_resolved_by_offset():
    path = fresh_db()
    try:
        import_text(sample_data.payload(), mode="replace", db_path=path)
        # srv-db 时钟慢 30s -> 修正 offset +30
        with session(path) as conn:
            conn.execute("UPDATE sources SET offset_seconds=30 WHERE name='srv-db'")
            stats = recompute(conn)
            rows = [r["kind"] for r in conn.execute("SELECT kind FROM conflicts")]
        # 区间冲突应消失（闭环冲突来自 edge.bad2，与 srv-db 无关，仍在）
        assert "interval" not in rows, rows
        assert "cycle" in rows
        assert stats["conflict_count"] >= 1
    finally:
        os.unlink(path)


def test_cycle_resolved_by_disabling_monotonic():
    path = fresh_db()
    try:
        import_text(sample_data.payload(), mode="replace", db_path=path)
        with session(path) as conn:
            conn.execute("UPDATE sources SET offset_seconds=30 WHERE name='srv-db'")
            conn.execute("UPDATE sources SET monotonic=0 WHERE name='srv-edge'")
            stats = recompute(conn)
            rows = [r["kind"] for r in conn.execute("SELECT kind FROM conflicts")]
        assert rows == [], rows
        assert stats["conflict_count"] == 0
    finally:
        os.unlink(path)


def test_interval_propagation_forces_order():
    """显式 before + 足够大的时钟偏差应让边变为 forced；偏差假设错误则 impossible。"""
    path = fresh_db()
    payload = """{
      "sources": [
        {"name": "a", "uncertainty_seconds": 0.1, "monotonic": false},
        {"name": "b", "uncertainty_seconds": 0.1, "monotonic": false}
      ],
      "events": [
        {"id": "x", "source": "a", "timestamp": "2024-01-01T00:00:00Z", "before": ["b/y"]},
        {"id": "y", "source": "b", "timestamp": "2024-01-01T00:00:10Z"}
      ]
    }"""
    try:
        import_text(payload, mode="replace", db_path=path)
        with session(path) as conn:
            st = conn.execute("""
                SELECT dc.status FROM derived_constraints dc
                JOIN events e1 ON e1.id=dc.from_event
                WHERE e1.ext_id='x'
            """).fetchone()
            assert st["status"] == "forced", st["status"]
        # 把 b 的时钟设为快 30s（offset=-30），y 真实时间变成 -20s -> x before y 不可能
        with session(path) as conn:
            conn.execute("UPDATE sources SET offset_seconds=-30 WHERE name='b'")
            recompute(conn)
            st = conn.execute("""
                SELECT dc.status FROM derived_constraints dc
                JOIN events e1 ON e1.id=dc.from_event
                WHERE e1.ext_id='x'
            """).fetchone()
            assert st["status"] == "impossible", st["status"]
    finally:
        os.unlink(path)


def test_merge_dedup_and_ndjson():
    path = fresh_db()
    line1 = '{"id":"m1","source":"s","timestamp":1700000000}'
    line2 = 'not-json'
    line3 = '{"id":"m2","source":"s","timestamp":1700000005}'
    try:
        stats = import_text("\n".join([line1, line2, line3]), mode="replace", db_path=path)
        assert stats["imported"] == 2
        assert any("第 2 行" in w for w in stats["warnings"])
        stats2 = import_text(line1.replace("1700000000", "1700000009"), mode="merge", db_path=path)
        assert stats2["imported"] == 1
        with session(path) as conn:
            n = conn.execute("SELECT COUNT(*) c FROM events WHERE ext_id='m1'").fetchone()["c"]
            assert n == 1
            ts = conn.execute("SELECT raw_ts FROM events WHERE ext_id='m1'").fetchone()["raw_ts"]
            assert abs(ts - 1700000009) < 1e-6
    finally:
        os.unlink(path)


def test_confidence_bounds():
    path = fresh_db()
    try:
        import_text(sample_data.payload(), mode="replace", db_path=path)
        with session(path) as conn:
            vals = [r["confidence"] for r in conn.execute("SELECT confidence FROM events")]
        assert all(0 <= v <= 1 for v in vals)
    finally:
        os.unlink(path)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as ex:  # noqa
            failed += 1
            print(f"FAIL {fn.__name__}: {ex}")
            import traceback; traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
