from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from app.core.config import settings
from app.schemas.responses import LiquidationResponse, MarketsResponse, PositionsResponse
from app.services.morpho_client import (
    MorphoClient,
    build_market_positions,
    build_markets_response,
    build_vault_position_from_v1,
    build_vault_position_from_v2,
    format_decimal,
    format_optional_decimal,
    normalize_lltv,
    safe_get,
    to_decimal,
)
from app.services.onchain import OnchainClient
from app.services.rewards_client import RewardsClient
from app.services.storage import MongoStorage

router = APIRouter(prefix="/api/v1/morpho", tags=["morpho"])

morpho_client = MorphoClient()
rewards_client = RewardsClient()
onchain_client = OnchainClient()
storage = MongoStorage()

SUPPORTED_CHAIN_IDS = set(settings.chain_configs().keys())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_chain_id(chain_id: int) -> int:
    if chain_id not in SUPPORTED_CHAIN_IDS:
        raise HTTPException(status_code=400, detail="Unsupported chainId")
    return chain_id


@router.get("/{address}/positions", response_model=PositionsResponse)
async def get_positions(address: str, chainId: int = Query(1, alias="chainId")):
    chain_id = validate_chain_id(chainId)

    user_data_task = morpho_client.fetch_user_by_address(chain_id, address)
    rewards_task = rewards_client.fetch_user_rewards(address)
    user_data, rewards_data = await asyncio.gather(user_data_task, rewards_task, return_exceptions=True)

    if isinstance(user_data, Exception):
        raise HTTPException(status_code=502, detail="Failed to fetch Morpho data")

    user = safe_get(user_data, "userByAddress")
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    state = safe_get(user, "state", {})
    summary = {
        "totalSupplyUsd": format_optional_decimal(
            to_decimal(safe_get(state, "vaultV2sAssetsUsd", 0))
            + to_decimal(safe_get(state, "vaultsAssetsUsd", 0))
        ),
        "totalBorrowUsd": format_optional_decimal(safe_get(state, "marketsBorrowAssetsUsd", 0)),
        "netWorthUsd": format_optional_decimal(
            to_decimal(safe_get(state, "marketsCollateralUsd", 0))
            - to_decimal(safe_get(state, "marketsBorrowAssetsUsd", 0))
            + to_decimal(safe_get(state, "vaultV2sAssetsUsd", 0))
            + to_decimal(safe_get(state, "vaultsAssetsUsd", 0))
        ),
    }

    vault_positions: List[Dict[str, Any]] = []
    for v1_position in safe_get(user, "vaultPositions", []) or []:
        vault_positions.append(await build_vault_position_from_v1(v1_position))

    v2_positions = safe_get(user, "vaultV2Positions", []) or []

    async def fetch_v2_position(position: Dict[str, Any]) -> Dict[str, Any]:
        vault = safe_get(position, "vault", {})
        adapters = safe_get(vault, "adapters", {})
        adapter_items = safe_get(adapters, "items", []) or []
        adapter_addresses = [safe_get(item, "address") for item in adapter_items if item]

        v1_addresses = await asyncio.gather(
            *[
                onchain_client.fetch_morpho_vault_v1(chain_id, addr)
                for addr in adapter_addresses
                if addr
            ],
            return_exceptions=True,
        )
        v1_address = None
        for result in v1_addresses:
            if isinstance(result, Exception):
                continue
            if result:
                v1_address = result
                break

        allocation_data = None
        if v1_address:
            try:
                allocation_data = await morpho_client.fetch_vault_by_address(chain_id, v1_address)
                allocation_data = safe_get(allocation_data, "vaultByAddress")
            except Exception:
                allocation_data = None

        return await build_vault_position_from_v2(position, allocation_data)

    if v2_positions:
        v2_results = await asyncio.gather(*[fetch_v2_position(p) for p in v2_positions])
        vault_positions.extend(v2_results)

    market_positions = build_market_positions(safe_get(user, "marketPositions", []) or [])

    unclaimed_rewards = []
    if not isinstance(rewards_data, Exception):
        unclaimed_rewards = await rewards_client.build_unclaimed_rewards(rewards_data)

    payload = {
        "address": safe_get(user, "address") or address,
        "protocol": "morpho",
        "chainId": chain_id,
        "timestamp": now_iso(),
        "summary": summary,
        "vaultPositions": vault_positions,
        "marketPositions": [
            {k: v for k, v in item.items() if k != "_extra"} for item in market_positions
        ],
        "rewards": {"unclaimedRewards": unclaimed_rewards},
    }

    storage.save_snapshot_background("positions", payload)

    return payload


@router.get("/{address}/liquidation", response_model=LiquidationResponse)
async def get_liquidation(address: str, chainId: int = Query(1, alias="chainId")):
    chain_id = validate_chain_id(chainId)

    user_data = await morpho_client.fetch_user_by_address(chain_id, address)
    user = safe_get(user_data, "userByAddress")
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    market_positions_raw = safe_get(user, "marketPositions", []) or []
    liquidation_positions: List[Dict[str, Any]] = []

    for position in market_positions_raw:
        state = safe_get(position, "state", {})
        market = safe_get(position, "market", {})
        loan = safe_get(market, "loanAsset", {})
        collateral = safe_get(market, "collateralAsset", {})

        health_factor = to_decimal(safe_get(position, "healthFactor", 0))
        if health_factor >= Decimal("2.0"):
            risk_level = "safe"
        elif health_factor >= Decimal("1.5"):
            risk_level = "medium"
        elif health_factor >= Decimal("1.2"):
            risk_level = "risky"
        else:
            risk_level = "critical"

        price_variation = to_decimal(safe_get(position, "priceVariationToLiquidationPrice", 0))
        price_drop_percent = format_decimal(price_variation * Decimal("100"), 2) + "%"

        collateral_price = to_decimal(safe_get(collateral, "priceUsd", 0))
        liquidation_price = collateral_price * (Decimal("1") + price_variation)

        collateral_decimals = int(safe_get(collateral, "decimals", 18) or 18)
        loan_decimals = int(safe_get(loan, "decimals", 18) or 18)
        collateral_amount = to_decimal(safe_get(state, "collateral", 0)) / (
            Decimal(10) ** collateral_decimals
        )
        borrow_amount = to_decimal(safe_get(state, "borrowAssets", 0)) / (
            Decimal(10) ** loan_decimals
        )

        liquidation_positions.append(
            {
                "marketId": safe_get(market, "uniqueKey"),
                "healthFactor": format_decimal(health_factor, 2),
                "riskLevel": risk_level,
                "lltv": normalize_lltv(safe_get(market, "lltv"), 2),
                "liquidationPrice": {
                    "collateralAsset": safe_get(collateral, "symbol"),
                    "debtAsset": safe_get(loan, "symbol"),
                    "currentPrice": format_decimal(collateral_price, 2),
                    "liquidationPrice": format_decimal(liquidation_price, 2),
                    "priceDropToLiquidation": price_drop_percent,
                },
                "collateralAtRisk": {
                    "asset": safe_get(collateral, "symbol"),
                    "amount": format_decimal(collateral_amount, collateral_decimals),
                    "amountUsd": format_optional_decimal(safe_get(state, "collateralUsd", 2), 2),
                },
                "debtToCover": {
                    "asset": safe_get(loan, "symbol"),
                    "amount": format_decimal(borrow_amount, loan_decimals),
                    "amountUsd": format_optional_decimal(safe_get(state, "borrowAssetsUsd", 0), 2),
                },
            }
        )

    payload = {
        "address": safe_get(user, "address") or address,
        "chainId": chain_id,
        "timestamp": now_iso(),
        "marketPositions": liquidation_positions,
    }

    storage.save_snapshot_background("liquidation", payload)

    return payload


@router.get("/markets", response_model=MarketsResponse)
async def get_markets(chainId: int = Query(1, alias="chainId")):
    chain_id = validate_chain_id(chainId)

    data = await morpho_client.fetch_markets(chain_id)
    payload = build_markets_response(data)
    payload = {
        "chainId": chain_id,
        "timestamp": now_iso(),
        **payload,
    }

    storage.save_snapshot_background("markets", payload)

    return payload
