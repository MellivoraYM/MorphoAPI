#!/usr/bin/env bash
set -euo pipefail

# API 服务（不跑定时任务），用于承接高并发请求
#
# 依赖环境变量：
#   MYSQL_URL
#   SCHEDULER_ENABLED=false
#   MORPHO_GRAPHQL_URL (optional)
#   MORPHO_REWARDS_URL (optional)
#   ETH_RPC_URL / ARB_RPC_URL / BASE_RPC_URL (optional)

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
WORKERS="${WORKERS:-4}"

export SCHEDULER_ENABLED="${SCHEDULER_ENABLED:-false}"

exec gunicorn \
  -k uvicorn.workers.UvicornWorker \
  -w "${WORKERS}" \
  -b "${HOST}:${PORT}" \
  app.main:app

