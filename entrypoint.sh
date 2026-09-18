#!/bin/sh
# 容器启动时完成依赖环境检查与 SQLite 数据库初始化，然后启动服务。
set -e

echo "[entrypoint] initializing SQLite database ..."
python -m app.database

echo "[entrypoint] starting uvicorn ..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
