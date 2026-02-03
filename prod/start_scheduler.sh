#!/usr/bin/env bash
set -euo pipefail

# Scheduler 服务（只跑定时任务），避免与 API worker 重复执行任务
#
# 依赖环境变量：
#   MYSQL_URL
#   SCHEDULER_ENABLED=true

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8001}"

export SCHEDULER_ENABLED="${SCHEDULER_ENABLED:-true}"

# 单 worker，确保只启动一个 scheduler 实例
exec gunicorn \
  -k uvicorn.workers.UvicornWorker \
  -w 1 \
  -b "${HOST}:${PORT}" \
  app.main:app

