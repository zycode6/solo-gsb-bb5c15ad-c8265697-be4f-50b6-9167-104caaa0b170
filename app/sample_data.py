"""内置示例数据：三台存在时钟偏差的服务器。

- srv-db 的时钟比参考时慢 30 秒（offset = +30），初始假设为 0 时，
  ``payment.success``（srv-db）与 ``order.created``（srv-api）之间的
  显式先后链会把时间区间挤压为空 —— 审计员把 srv-db 偏差调到 +30 后冲突消失。
- srv-edge 时钟快 15 秒（offset = -15），且其日志记录顺序真实可信（单调）。
- 另外预留了一个人工误标的反向因果链（edge.bad2 -> edge.bad1），
  与 srv-edge 的单调时钟假设形成闭环；在来源面板关闭其"单调时钟"后重算即可解除。
"""
from __future__ import annotations

import json

BASE = 1758000000.0  # 固定基准，保证演示可复现

SOURCES = [
    {"name": "srv-api",  "offset_seconds": 0.0,  "drift_ppm": 0.0, "uncertainty_seconds": 1.0, "trust": 0.9, "monotonic": True, "note": "API 网关，NTP 同步良好"},
    {"name": "srv-db",   "offset_seconds": 0.0,  "drift_ppm": 0.0, "uncertainty_seconds": 2.0, "trust": 0.75, "monotonic": True, "note": "数据库主机，NTP 失效，时钟慢约 30 秒"},
    {"name": "srv-edge", "offset_seconds": 0.0,  "drift_ppm": 0.0, "uncertainty_seconds": 3.0, "trust": 0.6, "monotonic": True, "note": "边缘节点，时钟快约 15 秒，存在错误互指标记"},
]

EVENTS = [
    # srv-api：参考时基准
    {"id": "api.1", "source": "srv-api", "timestamp": BASE + 0,  "type": "order.created",   "summary": "用户下单 #1001",      "before": ["api.2"]},
    {"id": "api.2", "source": "srv-api", "timestamp": BASE + 6,  "type": "payment.request", "summary": "发起支付请求",        "before": ["srv-db/db.1"], "jitter_seconds": 0.5},
    {"id": "api.3", "source": "srv-api", "timestamp": BASE + 24, "type": "order.confirmed", "summary": "订单确认",            "after": ["srv-db/db.2"]},

    # srv-db：真实时间比日志晚 30 秒（日志时间戳 = 真实时 - 30）
    {"id": "db.1", "source": "srv-db", "timestamp": BASE - 18, "type": "payment.success", "summary": "支付成功落库", "before": ["db.2"], "jitter_seconds": 1.0},
    {"id": "db.2", "source": "srv-db", "timestamp": BASE - 10, "type": "ledger.posted",   "summary": "分户账记账"},

    # srv-edge：日志时间戳 = 真实时 + 15
    {"id": "edge.1", "source": "srv-edge", "timestamp": BASE + 22, "type": "http.access", "summary": "边缘收到下单请求", "jitter_seconds": 0.2},
    {"id": "edge.2", "source": "srv-edge", "timestamp": BASE + 31, "type": "http.access", "summary": "边缘收到支付回调"},

    # 人工误标：bad2 显式声明早于 bad1，但源时钟单调假设又强制 bad1(40) 早于 bad2(42)，
    # 形成闭环冲突；在来源面板关闭 srv-edge 的"单调时钟"假设后重算，环即解除
    {"id": "edge.bad1", "source": "srv-edge", "timestamp": BASE + 40, "type": "note", "summary": "值班操作记录 A", "before": []},
    {"id": "edge.bad2", "source": "srv-edge", "timestamp": BASE + 42, "type": "note", "summary": "值班操作记录 B（人工标注：B 早于 A）", "before": ["edge.bad1"]},
]


def payload() -> str:
    return json.dumps({"sources": SOURCES, "events": EVENTS}, ensure_ascii=False, indent=2)
