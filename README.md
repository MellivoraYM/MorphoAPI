# Morpho Portfolio Tracker API

基于 FastAPI 的 Morpho Portfolio Tracker，提供 Positions / Liquidation / Markets 三个接口，支持并发请求与多链扩展（默认支持 Ethereum、Arbitrum、Base）。

## Ubuntu 部署

```bash
./setup.sh
```

启动服务（生产）：

```bash
source .venv/bin/activate
gunicorn -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:8000 app.main:app
```

## 压测脚本

准备 `targets.csv`（无表头）：

```
1,0x...
42161,0x...
8453,0x...
```

运行：

```bash
./load_test.sh -u http://127.0.0.1:8000 -e positions -f targets.csv -c 10
```

支持 `positions` / `liquidation`。

## 接口说明

- `GET /api/v1/morpho/{address}/positions?chainId=1`
- `GET /api/v1/morpho/{address}/liquidation?chainId=1`
- `GET /api/v1/morpho/markets?chainId=1`

`chainId` 参数可选，默认 `1`（Ethereum 主网）。

## 接口文档（Swagger/OpenAPI）

- Swagger UI: `http://<host>:8000/docs`
- ReDoc: `http://<host>:8000/redoc`
- OpenAPI JSON: `http://<host>:8000/openapi.json`

## MongoDB 持久化

- 默认启用（`MONGO_ENABLED=true`）
- 每次接口调用会写入一条快照到集合：`positions` / `liquidation` / `markets`
- 字段包含 `createdAt` 与接口返回完整 payload

## 环境变量（可选）

- `MONGO_ENABLED`（默认 `true`）
- `MONGO_URI`（默认 `mongodb://localhost:27017`）
- `MONGO_DB`（默认 `morpho`）
- `MORPHO_GRAPHQL_URL`（默认 `https://api.morpho.org/graphql`）
- `MORPHO_REWARDS_URL`（默认 `https://rewards.morpho.org/v1`）
- `ETH_RPC_URL`（默认 `wss://ethereum-rpc.publicnode.com`）
- `ARB_RPC_URL`（默认 `https://public-arb-mainnet.fastnode.io`）
- `BASE_RPC_URL`（默认 `https://base.drpc.org`）

## 参考文档

- Morpho API 文档：[https://docs.morpho.org/tools/offchain/api/get-started/](https://docs.morpho.org/tools/offchain/api/get-started/)
