from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.core.config import settings
from app.services.morpho_client import (
    MorphoClient,
    build_market_positions,
    build_markets_response,
    build_vault_position_from_v1,
    build_vault_position_from_v2,
    format_decimal,
    format_full_decimal,
    format_optional_decimal,
    normalize_lltv,
    safe_get,
    to_decimal,
    to_percent,
)
from app.services.onchain import OnchainClient
from app.services.rewards_client import RewardsClient
from app.services.storage import MySQLStorage

router = APIRouter(prefix="/api/v1/morpho", tags=["morpho"])
history_router = APIRouter(prefix="/api/v1/history/morpho", tags=["history"])
register_router = APIRouter(prefix="/api/v1", tags=["register"])
rewards_router = APIRouter(prefix="/api/v1/morpho", tags=["morpho-rewards"])

morpho_client = MorphoClient()
rewards_client = RewardsClient()
onchain_client = OnchainClient()
storage = MySQLStorage()

SUPPORTED_CHAIN_IDS = set(settings.chain_configs().keys())


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class APIError(Exception):
    def __init__(self, code: int, message: str, http_status: int = 400) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


def error_response(code: int, message: str, details: Optional[Any] = None) -> Dict[str, Any]:
    payload = {"code": code, "status": "error", "message": message}
    if details is not None:
        payload["details"] = details
    return payload


def validate_chain_id(chain_id: int) -> int:
    if chain_id not in SUPPORTED_CHAIN_IDS:
        raise APIError(4001, "Unsupported chainId", 400)
    return chain_id


class RegisterRequest(BaseModel):
    userAddressList: List[str]


def _day_bounds(ts: int) -> tuple[int, int]:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return int(start.timestamp()), int(end.timestamp())


def _parse_total_supply_usd(payload: Dict[str, Any]) -> Decimal:
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    return to_decimal(summary.get("totalSupplyUsd", 0))


def _calc_event_adjustments(
    address: str,
    chain_id: int,
    start_ts: int,
    end_ts: int,
    vault_address: Optional[str] = None,
) -> Decimal:
    vault_txs = storage.fetch_vault_transactions_by_time(address, chain_id, start_ts, end_ts)
    adjustment = Decimal("0")
    for tx in vault_txs:
        data = tx.data or {}
        if vault_address:
            tx_vault = (data.get("vaultAddress") or "").lower()
            if not tx_vault or tx_vault != vault_address.lower():
                continue
        amount_usd = to_decimal(data.get("priceUsd", 0))
        tx_type = tx.type
        if tx_type == "Deposit":
            adjustment -= amount_usd
        elif tx_type == "Withdraw":
            adjustment += amount_usd
        elif tx_type == "Transfer":
            direction = data.get("direction")
            if direction == "in":
                adjustment += amount_usd
            elif direction == "out":
                adjustment -= amount_usd
    return adjustment


def _extract_vault_balance_usd(payload: Dict[str, Any], vault_address: str) -> Optional[Decimal]:
    if not payload:
        return None
    for item in payload.get("vaultPositions", []) or []:
        if (item.get("vaultAddress") or "").lower() == vault_address.lower():
            return to_decimal(item.get("balanceUsd", 0))
    return None


def _compute_vault_daily_reward(
    address: str,
    chain_id: int,
    vault_address: str,
    target_ts: int,
    current_balance_usd: Decimal,
) -> Decimal:
    start_ts, end_ts = _day_bounds(target_ts)
    today_start, _ = _day_bounds(now_ts())
    effective_ts = end_ts if start_ts < today_start else target_ts

    baseline_snapshot = storage.fetch_positions_snapshot_at_or_after(address, chain_id, start_ts)
    if baseline_snapshot:
        baseline_balance = _extract_vault_balance_usd(baseline_snapshot.payload, vault_address)
        baseline_ts = int(baseline_snapshot.created_at.replace(tzinfo=timezone.utc).timestamp())
        baseline_ts = max(start_ts, baseline_ts)
    else:
        baseline_balance = None
        baseline_ts = start_ts

    if baseline_balance is None:
        fallback_snapshot = storage.fetch_positions_snapshot_before(address, chain_id, target_ts)
        baseline_balance = (
            _extract_vault_balance_usd(fallback_snapshot.payload, vault_address)
            if fallback_snapshot
            else current_balance_usd
        )

    current_snapshot = storage.fetch_positions_snapshot_before(address, chain_id, effective_ts)
    if current_snapshot:
        current_balance = _extract_vault_balance_usd(current_snapshot.payload, vault_address)
    else:
        current_balance = None
    if current_balance is None:
        current_balance = current_balance_usd

    adjustment = _calc_event_adjustments(address, chain_id, baseline_ts, effective_ts, vault_address)
    return current_balance - baseline_balance + adjustment


def _compute_daily_reward(
    address: str,
    chain_id: int,
    target_ts: int,
    current_total_supply_usd: Decimal,
) -> Decimal:
    start_ts, end_ts = _day_bounds(target_ts)
    today_start, _ = _day_bounds(now_ts())
    effective_ts = end_ts if start_ts < today_start else target_ts
    baseline_snapshot = storage.fetch_positions_snapshot_at_or_after(address, chain_id, start_ts)
    if baseline_snapshot:
        baseline = _parse_total_supply_usd(baseline_snapshot.payload)
        baseline_ts = int(baseline_snapshot.created_at.replace(tzinfo=timezone.utc).timestamp())
        baseline_ts = max(start_ts, baseline_ts)
    else:
        fallback_snapshot = storage.fetch_positions_snapshot_before(address, chain_id, target_ts)
        baseline = _parse_total_supply_usd(fallback_snapshot.payload) if fallback_snapshot else current_total_supply_usd
        baseline_ts = start_ts

    current_snapshot = storage.fetch_positions_snapshot_before(address, chain_id, effective_ts)
    if current_snapshot:
        current_total = _parse_total_supply_usd(current_snapshot.payload)
    else:
        current_total = current_total_supply_usd

    adjustment = _calc_event_adjustments(address, chain_id, baseline_ts, effective_ts)
    return current_total - baseline + adjustment


async def build_positions_payload_from_user(
    user: Dict[str, Any],
    address: str,
    chain_id: int,
    rewards_data: Any,
    target_ts: Optional[int] = None,
) -> Dict[str, Any]:
    state = safe_get(user, "state", {})
    current_total_supply_usd = (
        to_decimal(safe_get(state, "vaultV2sAssetsUsd", 0))
        + to_decimal(safe_get(state, "vaultsAssetsUsd", 0))
    )
    effective_ts = target_ts or now_ts()
    daily_reward = _compute_daily_reward(address, chain_id, effective_ts, current_total_supply_usd)
    summary = {
        "totalSupplyUsd": format_full_decimal(
            to_decimal(safe_get(state, "vaultV2sAssetsUsd", 0))
            + to_decimal(safe_get(state, "vaultsAssetsUsd", 0))
        ),
        "totalBorrowUsd": format_full_decimal(safe_get(state, "marketsBorrowAssetsUsd", 0)),
        "netWorthUsd": format_full_decimal(
            to_decimal(safe_get(state, "marketsCollateralUsd", 0))
            - to_decimal(safe_get(state, "marketsBorrowAssetsUsd", 0))
            + to_decimal(safe_get(state, "vaultV2sAssetsUsd", 0))
            + to_decimal(safe_get(state, "vaultsAssetsUsd", 0))
        ),
    }

    vault_positions: List[Dict[str, Any]] = []
    for v1_position in safe_get(user, "vaultPositions", []) or []:
        vault = safe_get(v1_position, "vault", {})
        vault_address = safe_get(vault, "address")
        current_balance_usd = to_decimal(safe_get(safe_get(v1_position, "state", {}), "assetsUsd", 0))
        vault_daily = None
        if vault_address:
            vault_daily = _compute_vault_daily_reward(
                address, chain_id, vault_address, effective_ts, current_balance_usd
            )
        vault_positions.append(
            await build_vault_position_from_v1(
                v1_position,
                format_full_decimal(vault_daily) if vault_daily is not None else None,
            )
        )

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

        vault = safe_get(position, "vault", {})
        vault_address = safe_get(vault, "address")
        current_balance_usd = to_decimal(safe_get(position, "assetsUsd", 0))
        vault_daily = None
        if vault_address:
            vault_daily = _compute_vault_daily_reward(
                address, chain_id, vault_address, effective_ts, current_balance_usd
            )
        return await build_vault_position_from_v2(
            position,
            allocation_data,
            format_full_decimal(vault_daily) if vault_daily is not None else None,
        )

    if v2_positions:
        v2_results = await asyncio.gather(*[fetch_v2_position(p) for p in v2_positions])
        vault_positions.extend(v2_results)

    market_positions = build_market_positions(safe_get(user, "marketPositions", []) or [])

    unclaimed_rewards = []
    if not isinstance(rewards_data, Exception):
        unclaimed_rewards = await rewards_client.build_unclaimed_rewards(rewards_data)

    return {
        "address": safe_get(user, "address") or address,
        "protocol": "morpho",
        "chainId": chain_id,
        "timestamp": now_ts(),
        "summary": summary,
        "vaultPositions": vault_positions,
        "marketPositions": [
            {k: v for k, v in item.items() if k != "_extra"} for item in market_positions
        ],
        "rewards": {"unclaimedRewards": unclaimed_rewards},
        "totalDailyReward": format_full_decimal(daily_reward),
    }


async def build_positions_payload(
    address: str, chain_id: int, target_ts: Optional[int] = None
) -> Dict[str, Any]:
    user_data_task = morpho_client.fetch_user_by_address(chain_id, address)
    rewards_task = rewards_client.fetch_user_rewards(address)
    user_data, rewards_data = await asyncio.gather(user_data_task, rewards_task, return_exceptions=True)

    if isinstance(user_data, Exception):
        raise APIError(5000, "Failed to fetch Morpho data", 502)

    user = safe_get(user_data, "userByAddress")
    if not user:
        raise APIError(4004, "No position data found for this user", 404)

    has_positions = any(
        safe_get(user, key)
        for key in ("vaultPositions", "vaultV2Positions", "marketPositions")
    )
    if not has_positions:
        raise APIError(4004, "No position data found for this user", 404)

    return await build_positions_payload_from_user(user, address, chain_id, rewards_data, target_ts)


def build_liquidation_from_user(user: Dict[str, Any], address: str, chain_id: int) -> Dict[str, Any]:
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
                    "amountUsd": format_optional_decimal(safe_get(state, "collateralUsd"), 2),
                },
                "debtToCover": {
                    "asset": safe_get(loan, "symbol"),
                    "amount": format_decimal(borrow_amount, loan_decimals),
                    "amountUsd": format_optional_decimal(safe_get(state, "borrowAssetsUsd", 0), 2),
                },
            }
        )

    return {
        "address": safe_get(user, "address") or address,
        "chainId": chain_id,
        "timestamp": now_ts(),
        "marketPositions": liquidation_positions,
    }


async def build_liquidation_payload(address: str, chain_id: int) -> Dict[str, Any]:
    user_data = await morpho_client.fetch_user_by_address(chain_id, address)
    user = safe_get(user_data, "userByAddress")
    if not user:
        raise APIError(4004, "User not found", 404)

    return build_liquidation_from_user(user, address, chain_id)


async def build_markets_payload(chain_id: int) -> Dict[str, Any]:
    data = await morpho_client.fetch_markets(chain_id)
    payload = build_markets_response(data)
    return {
        "chainId": chain_id,
        "timestamp": now_ts(),
        **payload,
    }


def normalize_vault_v1_type(value: str) -> str:
    mapping = {
        "MetaMorphoDeposit": "Deposit",
        "MetaMorphoWithdraw": "Withdraw",
        "MetaMorphoTransfer": "Transfer",
        "MetaMorphoFee": "Withdraw",
    }
    return mapping.get(value, value)


async def fetch_and_store_transactions(address: str, chain_id: int) -> Dict[str, List[Dict[str, Any]]]:
    v2_data, v1_data, market_data = await asyncio.gather(
        morpho_client.fetch_vault_v2_transactions(chain_id, address),
        morpho_client.fetch_vault_v1_transactions(chain_id, address),
        morpho_client.fetch_market_transactions(chain_id, address),
    )

    vault_transactions: List[Dict[str, Any]] = []
    v2_items = safe_get(v2_data, "vaultV2transactions", {}).get("items", []) or []
    for item in v2_items:
        vault = safe_get(item, "vault", {})
        asset = safe_get(vault, "asset", {})
        decimals = int(safe_get(asset, "decimals", 18) or 18)
        data = safe_get(item, "data", {})
        assets = safe_get(data, "assets")
        amount = to_decimal(assets) / (Decimal(10) ** decimals) if assets is not None else Decimal("0")
        price = to_decimal(safe_get(asset, "priceUsd", 0))
        direction = None
        if safe_get(item, "type") == "Transfer":
            from_addr = safe_get(data, "from")
            to_addr = safe_get(data, "to")
            if from_addr and from_addr.lower() == address.lower():
                direction = "out"
            elif to_addr and to_addr.lower() == address.lower():
                direction = "in"
        payload = {
            "address": address,
            "chainId": chain_id,
            "type": safe_get(item, "type"),
            "txHash": safe_get(item, "txHash"),
            "timestamp": int(safe_get(item, "timestamp", 0) or 0),
            "blockNumber": safe_get(item, "blockNumber"),
            "data": {
                "vaultAddress": safe_get(vault, "address"),
                "asset": safe_get(asset, "symbol"),
                "amount": float(amount),
                "priceUsd": float(amount * price),
                "direction": direction,
            },
        }
        vault_transactions.append(payload)

    v1_items = safe_get(v1_data, "transactions", {}).get("items", []) or []
    for item in v1_items:
        data = safe_get(item, "data", {})
        vault = safe_get(data, "vault", {})
        asset = safe_get(vault, "asset", {})
        decimals = int(safe_get(asset, "decimals", 18) or 18)
        assets = safe_get(data, "assets")
        amount = to_decimal(assets) / (Decimal(10) ** decimals) if assets is not None else Decimal("0")
        assets_usd = safe_get(data, "assetsUsd")
        payload = {
            "address": address,
            "chainId": chain_id,
            "type": normalize_vault_v1_type(safe_get(item, "type")),
            "txHash": safe_get(item, "hash"),
            "timestamp": int(safe_get(item, "timestamp", 0) or 0),
            "blockNumber": safe_get(item, "blockNumber"),
            "data": {
                "vaultAddress": safe_get(vault, "address"),
                "asset": safe_get(asset, "symbol"),
                "amount": float(amount),
                "priceUsd": float(assets_usd) if assets_usd is not None else None,
            },
        }
        vault_transactions.append(payload)

    market_transactions: List[Dict[str, Any]] = []
    market_items = safe_get(market_data, "transactions", {}).get("items", []) or []
    for item in market_items:
        data = safe_get(item, "data", {})
        market = safe_get(data, "market", {})
        collateral_asset = safe_get(market, "collateralAsset", {})
        loan_asset = safe_get(market, "loanAsset", {})
        tx_type = safe_get(item, "type")

        is_collateral = tx_type in ("MarketSupplyCollateral", "MarketWithdrawCollateral")
        asset = collateral_asset if is_collateral else loan_asset
        decimals = int(safe_get(asset, "decimals", 18) or 18)

        if tx_type == "MarketLiquidation":
            assets = safe_get(data, "repaidAssets")
            assets_usd = safe_get(data, "repaidAssetsUsd")
        else:
            assets = safe_get(data, "assets")
            assets_usd = safe_get(data, "assetsUsd")

        amount = to_decimal(assets) / (Decimal(10) ** decimals) if assets is not None else Decimal("0")

        market_transactions.append(
            {
                "address": address,
                "chainId": chain_id,
                "type": tx_type,
                "txHash": safe_get(item, "hash"),
                "timestamp": int(safe_get(item, "timestamp", 0) or 0),
                "blockNumber": safe_get(item, "blockNumber"),
                "data": {
                    "asset": safe_get(asset, "symbol"),
                    "amount": float(amount),
                    "priceUsd": float(assets_usd) if assets_usd is not None else None,
                },
            }
        )

    await asyncio.to_thread(storage.save_vault_transactions, vault_transactions)
    await asyncio.to_thread(storage.save_market_transactions, market_transactions)

    return {
        "vaultTransactions": vault_transactions,
        "marketTransactions": market_transactions,
    }


async def build_positions_history_payload(address: str, chain_id: int) -> Dict[str, Any]:
    user_data = await morpho_client.fetch_user_by_address(chain_id, address)
    user = safe_get(user_data, "userByAddress")
    if not user:
        raise APIError(4004, "User not found", 404)

    vault_positions = []
    for item in safe_get(user, "vaultPositions", []) or []:
        state = safe_get(item, "state", {})
        vault = safe_get(item, "vault", {})
        vault_positions.append(
            {
                "vaultAddress": safe_get(vault, "address"),
                "vaultName": safe_get(vault, "name"),
                "balance": format_optional_decimal(safe_get(state, "assets"), 2),
                "balanceUsd": format_full_decimal(safe_get(state, "assetsUsd")),
                "apy": to_percent(safe_get(safe_get(vault, "state", {}), "avgNetApy", 0), 2),
            }
        )

    for item in safe_get(user, "vaultV2Positions", []) or []:
        vault = safe_get(item, "vault", {})
        vault_positions.append(
            {
                "vaultAddress": safe_get(vault, "address"),
                "vaultName": safe_get(vault, "name"),
                "balance": format_optional_decimal(safe_get(item, "assets"), 2),
                "balanceUsd": format_full_decimal(safe_get(item, "assetsUsd")),
                "apy": to_percent(safe_get(vault, "avgNetApy", 0), 2),
            }
        )

    market_positions = []
    for item in safe_get(user, "marketPositions", []) or []:
        state = safe_get(item, "state", {})
        market = safe_get(item, "market", {})
        loan = safe_get(market, "loanAsset", {})
        collateral = safe_get(market, "collateralAsset", {})
        market_name = f"{safe_get(collateral, 'symbol', 'N/A')}/{safe_get(loan, 'symbol', 'N/A')}"
        market_positions.append(
            {
                "marketId": safe_get(market, "uniqueKey"),
                "marketName": market_name,
                "healthFactor": format_optional_decimal(safe_get(item, "healthFactor"), 2),
                "collateralUsd": format_full_decimal(safe_get(state, "collateralUsd")),
                "borrowUsd": format_full_decimal(safe_get(state, "borrowAssetsUsd")),
            }
        )

    timestamps = []
    for item in safe_get(user, "vaultPositions", []) or []:
        timestamps.append(safe_get(safe_get(item, "state", {}), "timestamp"))
    for item in safe_get(user, "marketPositions", []) or []:
        timestamps.append(safe_get(safe_get(item, "state", {}), "timestamp"))
    timestamp = int(max((t or 0) for t in timestamps) or 0)
    if timestamp == 0:
        timestamp = int(datetime.now(timezone.utc).timestamp())

    state = safe_get(user, "state", {})
    current_total_supply_usd = (
        to_decimal(safe_get(state, "vaultV2sAssetsUsd", 0))
        + to_decimal(safe_get(state, "vaultsAssetsUsd", 0))
    )
    daily_reward = _compute_daily_reward(address, chain_id, timestamp, current_total_supply_usd)

    return {
        "address": safe_get(user, "address") or address,
        "chainId": chain_id,
        "timestamp": timestamp,
        "vaultPositions": vault_positions,
        "marketPositions": market_positions,
        "totalDailyReward": format_full_decimal(daily_reward),
    }
@router.get("/{address}/positions")
async def get_positions(
    address: str,
    chainId: int = Query(1, alias="chainId"),
    timestamp: Optional[int] = Query(None, alias="timestamp"),
):
    chain_id = validate_chain_id(chainId)
    payload = await build_positions_payload(address, chain_id, timestamp)
    await asyncio.to_thread(storage.save_positions_snapshot, payload)
    return payload


@router.get("/{address}/liquidation")
async def get_liquidation(address: str, chainId: int = Query(1, alias="chainId")):
    chain_id = validate_chain_id(chainId)
    payload = await build_liquidation_payload(address, chain_id)
    await asyncio.to_thread(storage.save_liquidation_snapshot, payload)
    return payload


@router.get("/markets")
async def get_markets(chainId: int = Query(1, alias="chainId")):
    chain_id = validate_chain_id(chainId)

    payload = await build_markets_payload(chain_id)
    await asyncio.to_thread(storage.save_markets_snapshot, payload)
    return payload


@rewards_router.get("/{address}/rewards")
async def get_rewards(
    address: str,
    chainId: int = Query(1, alias="chainId"),
    timestamp: Optional[int] = Query(None, alias="timestamp"),
):
    chain_id = validate_chain_id(chainId)
    target_ts = timestamp or now_ts()

    latest = storage.fetch_latest_positions_snapshot(address, chain_id)
    total_daily_reward = "0"
    latest_ts = target_ts
    if latest:
        latest_ts = int(latest.snapshot_ts or int(latest.created_at.replace(tzinfo=timezone.utc).timestamp()))
        total_daily_reward = latest.total_daily_reward.to_eng_string() if latest.total_daily_reward else "0"

    start_ts = int((datetime.fromtimestamp(target_ts, tz=timezone.utc) - timedelta(days=30)).timestamp())
    snapshots = storage.fetch_positions_snapshots_in_range(address, chain_id, start_ts, target_ts)

    day_map: Dict[int, PositionsSnapshot] = {}
    for snap in snapshots:
        snap_ts = int(snap.snapshot_ts or int(snap.created_at.replace(tzinfo=timezone.utc).timestamp()))
        day_start, _ = _day_bounds(snap_ts)
        existing = day_map.get(day_start)
        if not existing or snap_ts >= int(existing.snapshot_ts or int(existing.created_at.replace(tzinfo=timezone.utc).timestamp())):
            day_map[day_start] = snap

    daily_list = []
    for day_start in sorted(day_map.keys()):
        snap = day_map[day_start]
        snap_ts = int(snap.snapshot_ts or int(snap.created_at.replace(tzinfo=timezone.utc).timestamp()))
        daily_reward = snap.total_daily_reward.to_eng_string() if snap.total_daily_reward else "0"
        daily_list.append(
            {
                "timestamp": snap_ts,
                "totalDailyReward": daily_reward,
            }
        )

    return {
        "address": address,
        "protocol": "morpho",
        "chainId": chain_id,
        "timestamp": latest_ts,
        "totalDailyRewards": total_daily_reward,
        "totalDailyRewardsIn30D": daily_list,
    }


@register_router.post("/{protocol}/register")
async def register_address(protocol: str, body: RegisterRequest):
    if protocol != "morpho":
        raise APIError(4001, "Unsupported protocol", 400)
    inserted, skipped = await asyncio.to_thread(
        storage.register_addresses, protocol, body.userAddressList, None
    )
    # Fetch and persist transactions immediately after registration (only for new addresses).
    for chain_id in SUPPORTED_CHAIN_IDS:
        for address in inserted:
            await fetch_and_store_transactions(address, chain_id)
    if skipped:
        return {
            "message": "Some addresses were already registered",
            "registered": inserted,
            "skipped": skipped,
        }
    return {"registered": inserted, "skipped": []}


@history_router.get("/{address}/event")
async def get_history_events(
    address: str,
    chainId: int = Query(1, alias="chainId"),
    limit: int = Query(100, ge=1, le=500),
):
    chain_id = validate_chain_id(chainId)
    payload = await fetch_and_store_transactions(address, chain_id)
    vault_items = payload["vaultTransactions"][:limit]
    market_items = payload["marketTransactions"][:limit]
    return {
        "address": address,
        "chainId": chain_id,
        "vaultTransactions": vault_items,
        "marketTransactions": market_items,
    }


def bucket_timestamp(ts: int, interval: str) -> int:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if interval == "hour":
        bucket = dt.replace(minute=0, second=0, microsecond=0)
    elif interval == "day":
        bucket = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif interval == "week":
        bucket = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        bucket = bucket.replace(day=bucket.day - bucket.weekday())
    else:
        bucket = dt
    return int(bucket.timestamp())


@history_router.get("/{address}/positions")
async def get_history_positions(
    address: str,
    chainId: int = Query(1, alias="chainId"),
    startTime: int = Query(..., alias="startTime"),
    endTime: int = Query(..., alias="endTime"),
    interval: str = Query("day", alias="interval"),
):
    chain_id = validate_chain_id(chainId)
    rows = await asyncio.to_thread(storage.fetch_positions_history, address, chain_id, startTime, endTime)
    buckets: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        bucket_ts = bucket_timestamp(int(row.timestamp), interval)
        buckets[bucket_ts] = row.payload

    data_points = []
    for bucket_ts in sorted(buckets.keys()):
        payload = buckets[bucket_ts]
        data_points.append(
            {
                "timestamp": bucket_ts,
                "vaultPositions": payload.get("vaultPositions", []),
                "marketPositions": payload.get("marketPositions", []),
                "totalDailyReward": payload.get("totalDailyReward"),
            }
        )

    return {
        "address": address,
        "chainId": chain_id,
        "interval": interval,
        "dataPoints": data_points,
    }
