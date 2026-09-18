"""API 端到端冒烟脚本：python tests/smoke_api.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_db = tempfile.mktemp(suffix=".db")
os.environ["AUDIT_DB"] = _db

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

with TestClient(app) as c:
    st = c.get("/api/state").json()
    assert st["stats"]["event_count"] == 0

    st = c.post("/api/sample?mode=replace").json()
    assert st["stats"]["event_count"] == 9
    assert st["stats"]["conflict_count"] >= 2
    print("imported:", st["import"]["imported"], "conflicts:", st["stats"]["conflict_count"],
          "kinds:", [x["kind"] for x in st["conflicts"]])

    db = next(s for s in st["sources"] if s["name"] == "srv-db")
    edge = next(s for s in st["sources"] if s["name"] == "srv-edge")

    st2 = c.patch(f"/api/sources/{db['id']}", json={"offset_seconds": 30}).json()
    assert "interval" not in [x["kind"] for x in st2["conflicts"]]
    print("after db offset +30 kinds:", [x["kind"] for x in st2["conflicts"]])

    st3 = c.patch(f"/api/sources/{edge['id']}", json={"monotonic": False}).json()
    assert st3["stats"]["conflict_count"] == 0
    assert st3["stats"]["impossible_edge_count"] == 0
    print("after edge monotonic=false -> conflicts 0, avg conf", st3["stats"]["avg_confidence"])

    r = c.get("/api/export")
    assert r.status_code == 200
    j = r.json()
    assert "raw_records" in j and "model" in j
    print("export keys:", sorted(j.keys()))

    assert c.get("/").status_code == 200
    assert "审计台" in c.get("/").text
    assert c.get("/app.js").status_code == 200
    assert c.get("/styles.css").status_code == 200

    assert c.post("/api/import", data={"mode": "replace", "payload": "{bad"}).status_code == 400
    assert c.post("/api/sources/999", json={"offset_seconds": 1}).status_code == 404
    assert c.patch(f"/api/sources/{db['id']}", json={"trust": 5}).status_code == 422

    nd = ('{"id":"z1","source":"new-svc","timestamp":1700000000}\n'
          '{"id":"z2","source":"new-svc","timestamp":1700000002}')
    r = c.post("/api/import", data={"mode": "merge", "payload": nd})
    assert r.status_code == 200 and r.json()["stats"]["event_count"] == 11
    print("merge NDJSON -> event_count", r.json()["stats"]["event_count"])

    st = c.post("/api/reset").json()
    assert st["stats"]["event_count"] == 0

os.unlink(_db)
print("ALL SMOKE CHECKS PASSED")
