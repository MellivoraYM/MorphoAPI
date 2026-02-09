from __future__ import annotations

import asyncio
import warnings
from typing import Dict, List

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes.morpho import (
    APIError,
    SUPPORTED_CHAIN_IDS,
    build_liquidation_from_user,
    build_markets_payload,
    build_positions_history_payload,
    build_positions_payload,
    build_positions_payload_from_user,
    error_response,
    fetch_and_store_transactions,
    history_router,
    rewards_router,
    morpho_client,
    register_router,
    rewards_client,
    router as morpho_router,
    storage,
)
from app.core.config import settings

try:
    from urllib3.exceptions import NotOpenSSLWarning
except Exception:  # pragma: no cover - best effort for old urllib3 versions
    NotOpenSSLWarning = None

if NotOpenSSLWarning is not None:
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)

app = FastAPI(
    title="Morpho Portfolio Tracker",
    description=(
        "Provides positions, liquidation risk, and markets data for Morpho. "
        "Supports chainId=1/42161/8453 via query parameter."
    ),
    version="1.0.0",
    contact={"name": "Morpho API Maintainer"},
)

app.include_router(morpho_router)
app.include_router(history_router)
app.include_router(register_router)
app.include_router(rewards_router)

scheduler = AsyncIOScheduler()


@app.exception_handler(APIError)
async def handle_api_error(_: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=error_response(exc.code, exc.message),
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=error_response(4000, "Validation error", details=exc.errors()),
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "Request error"
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(4000, message),
    )


def _collect_addresses_by_chain() -> Dict[int, List[str]]:
    rows = storage.list_registered("morpho")
    mapping: Dict[int, List[str]] = {cid: [] for cid in SUPPORTED_CHAIN_IDS}
    for row in rows:
        if row.chain_id is None:
            for chain_id in SUPPORTED_CHAIN_IDS:
                mapping[chain_id].append(row.address)
        else:
            mapping.setdefault(row.chain_id, []).append(row.address)
    return mapping


async def snapshot_job() -> None:
    mapping = _collect_addresses_by_chain()
    for chain_id, addresses in mapping.items():
        for address in addresses:
            try:
                user_data_task = morpho_client.fetch_user_by_address(chain_id, address)
                rewards_task = rewards_client.fetch_user_rewards(address)
                user_data, rewards_data = await asyncio.gather(
                    user_data_task, rewards_task, return_exceptions=True
                )
                if isinstance(user_data, Exception):
                    continue
                user = user_data.get("userByAddress")
                if not user:
                    continue
                positions_payload = await build_positions_payload_from_user(
                    user, address, chain_id, rewards_data
                )
                await asyncio.to_thread(storage.save_positions_snapshot, positions_payload)
                liquidation_payload = build_liquidation_from_user(user, address, chain_id)
                await asyncio.to_thread(storage.save_liquidation_snapshot, liquidation_payload)
            except Exception:
                continue


async def markets_snapshot_job() -> None:
    for chain_id in SUPPORTED_CHAIN_IDS:
        try:
            markets_payload = await build_markets_payload(chain_id)
            await asyncio.to_thread(storage.save_markets_snapshot, markets_payload)
        except Exception:
            continue


async def transactions_job() -> None:
    mapping = _collect_addresses_by_chain()
    for chain_id, addresses in mapping.items():
        for address in addresses:
            try:
                await fetch_and_store_transactions(address, chain_id)
            except Exception:
                continue


async def hourly_positions_job() -> None:
    mapping = _collect_addresses_by_chain()
    for chain_id, addresses in mapping.items():
        for address in addresses:
            try:
                payload = await build_positions_history_payload(address, chain_id)
                await asyncio.to_thread(storage.save_positions_history, payload)
            except Exception:
                continue


@app.on_event("startup")
async def startup_event() -> None:
    if settings.scheduler_enabled:
        scheduler.add_job(
            snapshot_job,
            "cron",
            minute="*",
            second=0,
            id="snapshots",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=30,
        )
        scheduler.add_job(
            markets_snapshot_job,
            "cron",
            minute="*/5",
            second=10,
            id="markets_snapshots",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=30,
        )
        scheduler.add_job(
            hourly_positions_job,
            "cron",
            minute=0,
            second=0,
            id="positions_history",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        scheduler.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    if settings.scheduler_enabled:
        scheduler.shutdown(wait=False)
    await morpho_client.close()
    await rewards_client.close()
    storage.close()
