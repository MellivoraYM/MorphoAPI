# Morpho Portfolio Tracker API

基于 FastAPI 的 Morpho Portfolio Tracker，提供 Positions / Liquidation / Markets 三个接口，支持并发请求与多链扩展（默认支持 Ethereum、Arbitrum、Base）。

## Ubuntu 部署

```bash
./setup.sh
```

启动服务（生产，推荐“两套服务”）：

```bash
source .venv/bin/activate
export MYSQL_URL="mysql+pymysql://<user>:<password>@127.0.0.1:3306/morpho"

# 1) scheduler（单实例，避免重复入库）
SCHEDULER_ENABLED=true  gunicorn -k uvicorn.workers.UvicornWorker -w 1 -b 0.0.0.0:8001 app.main:app

# 2) API（多 worker，高并发）
SCHEDULER_ENABLED=false gunicorn -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8000 app.main:app
```

也可以使用仓库内脚本与 systemd 模板（见 `prod/` 目录）。

建议在生产环境仅启用单个调度器实例，避免多 worker 或 reload 重复入库：

## 接口说明

- `GET /api/v1/morpho/{address}/positions?chainId=1`
- `GET /api/v1/morpho/{address}/positions?chainId=1&timestamp=1700000000`
- `GET /api/v1/morpho/{address}/liquidation?chainId=1`
- `GET /api/v1/morpho/markets?chainId=1`
- `POST /api/v1/morpho/register`
- `GET /api/v1/history/morpho/{address}/event?chainId=1`
- `GET /api/v1/history/morpho/{address}/positions?chainId=1&startTime=...&endTime=...&interval=day`

`chainId` 参数可选，默认 `1`（Ethereum 主网）。

## 接口文档（Swagger/OpenAPI）

- Swagger UI: `http://<host>:8000/docs`
- ReDoc: `http://<host>:8000/redoc`
- OpenAPI JSON: `http://<host>:8000/openapi.json`

## MySQL 持久化

- 使用 MySQL 8，数据库默认 `morpho`
- 定时任务每分钟写入快照（positions/liquidation）
- markets 快照每 5 分钟写入一次
- 定时任务每小时写入历史仓位数据
- 历史交易记录仅在调用 `GET /api/v1/history/morpho/{address}/event` 或注册时拉取并去重入库

## positions 补充字段

- `riskLevel`：基于 `liquidityUsd / assetsUsd` 判断（Low/Medium/High）
- `dailyReward`：基于指定日零点到当前的总资产增量（支持 `timestamp` 参数）

## 环境变量（可选）

- `MYSQL_URL`（默认 `mysql+pymysql://<user>:<password>@127.0.0.1:3306/morpho`）
- `SCHEDULER_ENABLED`（默认 `true`）
- `MORPHO_GRAPHQL_URL`（默认 `https://api.morpho.org/graphql`）
- `MORPHO_REWARDS_URL`（默认 `https://rewards.morpho.org/v1`）
- `ETH_RPC_URL`（默认 `wss://ethereum-rpc.publicnode.com`）
- `ARB_RPC_URL`（默认 `https://public-arb-mainnet.fastnode.io`）
- `BASE_RPC_URL`（默认 `https://base.drpc.org`）

## 参考文档

- Morpho API 文档：[https://docs.morpho.org/tools/offchain/api/get-started/](https://docs.morpho.org/tools/offchain/api/get-started/)
