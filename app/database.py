"""SQLite 连接与 schema 初始化。

启动时（FastAPI lifespan）会调用 ``init_db``，保证容器一启动数据库
与表结构就已经就绪，无需审计员手动初始化。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

# 允许通过环境变量覆盖数据库位置（Docker 中挂卷到 /data）。
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "audit.db")
DB_PATH = os.environ.get("AUDIT_DB", DEFAULT_DB)

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS sources (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        name                TEXT UNIQUE NOT NULL,
        offset_seconds      REAL NOT NULL DEFAULT 0.0,   -- 参考时 = 源时钟 + offset
        drift_ppm           REAL NOT NULL DEFAULT 0.0,   -- 相对漂移（百万分率）
        uncertainty_seconds REAL NOT NULL DEFAULT 2.0,   -- 时钟偏差假设的不确定半径
        trust               REAL NOT NULL DEFAULT 0.8,   -- 来源可信度 0~1
        monotonic           INTEGER NOT NULL DEFAULT 1,  -- 是否假设源时钟单调（同源按序）
        note                TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ext_id        TEXT,                              -- 导入文件中的原始 ID
        source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        seq           INTEGER NOT NULL DEFAULT 0,        -- 同源内的导入序号
        event_type    TEXT NOT NULL DEFAULT '',
        summary       TEXT NOT NULL DEFAULT '',
        raw_ts        REAL NOT NULL,                     -- 源时钟时间戳（epoch 秒）
        jitter        REAL NOT NULL DEFAULT 0.0,         -- 单条记录时间戳抖动半径（秒）
        raw_json      TEXT NOT NULL DEFAULT '{}',        -- 原始记录（审计留证）
        -- 以下为派生字段，每次重算覆盖 --
        ref_mid       REAL,
        ref_lower     REAL,
        ref_upper     REAL,
        confidence    REAL,
        ordinal       INTEGER NOT NULL DEFAULT 0,
        conflict_flag INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS links (
        from_event INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        to_event   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        kind       TEXT NOT NULL DEFAULT 'explicit',     -- explicit = JSON 中 before/after
        PRIMARY KEY (from_event, to_event)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conflicts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        kind       TEXT NOT NULL,            -- cycle | interval
        event_ids  TEXT NOT NULL,            -- 冲突链上的 ext_id（JSON 数组，按顺序）
        detail     TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS derived_constraints (
        from_event INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        to_event   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        origin     TEXT NOT NULL,            -- monotonic | explicit
        status     TEXT NOT NULL,            -- forced | ambiguous | impossible
        PRIMARY KEY (from_event, to_event)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_ext ON events(ext_id)",
]


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)
    conn.commit()


def reset_workspace(conn: sqlite3.Connection) -> None:
    """清空当前审计工作区（顺序受外键约束保护）。"""
    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM derived_constraints")
    conn.execute("DELETE FROM links")
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM sources")
    conn.commit()


if __name__ == "__main__":  # 允许 entrypoint 预先初始化数据库
    with session() as conn:
        init_db(conn)
    print(f"database initialized at {os.path.abspath(DB_PATH)}")
