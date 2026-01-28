from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

import httpx

from app.core.config import settings

USER_BY_ADDRESS_QUERY = """
query UserByAddress($chainId: Int!, $address: String!) {
  userByAddress(chainId: $chainId, address: $address) {
    address
    state {
      vaultV2sAssetsUsd
      vaultsAssetsUsd
      marketsBorrowAssetsUsd
      marketsCollateralUsd
    }
    vaultPositions {
      state {
        shares
        assets
        assetsUsd
      }
      vault {
        address
        name
        asset {
          address
          symbol
        }
        state {
          curators {
            name
          }
          avgApy
          avgNetApy
          rewards {
            supplyApr
            asset {
              symbol
              address
            }
          }
          fee
          totalAssetsUsd
          allocation {
            market {
              uniqueKey
              collateralAsset {
                symbol
                address
              }
              loanAsset {
                symbol
                address
              }
              state {
                avgNetSupplyApy
                rewards {
                  supplyApr
                  asset {
                    symbol
                    address
                  }
                }
              }
            }
            supplyAssetsUsd
            supplyCapUsd
          }
        }
      }
    }
    vaultV2Positions {
      shares
      assets
      assetsUsd
      vault {
        address
        name
        curators {
          items {
            name
          }
        }
        asset {
          symbol
          address
        }
        avgApy
        avgNetApy
        rewards {
          supplyApr
          asset {
            symbol
            address
          }
        }
        managementFee
        performanceFee
        totalAssetsUsd
        adapters {
          items {
            address
          }
        }
      }
    }
    marketPositions {
      state {
        supplyShares
        supplyAssets
        supplyAssetsUsd
        borrowShares
        borrowAssets
        borrowAssetsUsd
        collateral
        collateralUsd
      }
      healthFactor
      priceVariationToLiquidationPrice
      market {
        uniqueKey
        loanAsset {
          symbol
          address
          priceUsd
          decimals
        }
        collateralAsset {
          symbol
          address
          priceUsd
          decimals
        }
        oracle {
          address
        }
        irmAddress
        lltv
        state {
          borrowApy
          avgBorrowApy
          avgNetBorrowApy
        }
      }
    }
  }
}
"""

VAULT_BY_ADDRESS_QUERY = """
query VaultByAddress($chainId: Int!, $address: String!) {
  vaultByAddress(chainId: $chainId, address: $address) {
    address
    state {
      totalAssetsUsd
      allocation {
        market {
          uniqueKey
          collateralAsset {
            symbol
            address
          }
          loanAsset {
            symbol
            address
          }
          state {
            avgNetSupplyApy
            rewards {
              supplyApr
              asset {
                symbol
                address
              }
            }
          }
        }
        supplyAssetsUsd
        supplyCapUsd
      }
    }
  }
}
"""

MARKETS_QUERY = """
query MarketsAndVaults($chainIds: [Int!]) {
  vaults(first: 100, orderBy: TotalAssetsUsd, orderDirection: Desc, where: { chainId_in: $chainIds }) {
    items {
      address
      name
      asset {
        symbol
        address
      }
      state {
        curators {
          name
        }
        totalAssets
        totalAssetsUsd
        avgApy
        avgNetApy
        rewards {
          supplyApr
        }
        fee
      }
    }
  }
  vaultV2s(first: 100, orderBy: TotalAssetsUsd, orderDirection: Desc, where: { chainId_in: $chainIds }) {
    items {
      address
      name
      curators {
        items {
          name
        }
      }
      asset {
        symbol
        address
      }
      totalAssets
      totalAssetsUsd
      avgApy
      avgNetApy
      rewards {
        supplyApr
      }
      managementFee
      performanceFee
    }
  }
  markets(first: 100, orderBy: SizeUsd, orderDirection: Desc, where: { chainId_in: $chainIds }) {
    items {
      uniqueKey
      loanAsset {
        symbol
        address
      }
      collateralAsset {
        symbol
        address
      }
      lltv
      state {
        supplyAssets
        supplyAssetsUsd
        borrowAssets
        borrowAssetsUsd
        utilization
        avgNetSupplyApy
        avgNetBorrowApy
      }
    }
  }
}
"""


def safe_get(data: Any, key: str, default: Any = None) -> Any:
    if not isinstance(data, dict):
        return default
    return data.get(key, default)


def to_decimal(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def format_decimal(value: Decimal, decimals: int = 2) -> str:
    if not value.is_finite():
        return "0"
    quantize_exp = Decimal("1") if decimals == 0 else Decimal(f"1e-{decimals}")
    try:
        return str(value.quantize(quantize_exp))
    except InvalidOperation:
        return str(value)


def to_percent(value: Any, decimals: int = 2) -> str:
    dec = to_decimal(value) * Decimal("100")
    return format_decimal(dec, decimals)


def normalize_lltv(value: Any, decimals: int = 2) -> str:
    dec = to_decimal(value)
    if dec > 1:
        dec = dec / Decimal("1e18")
    return format_decimal(dec, decimals)


def format_optional_decimal(value: Any, decimals: int = 2) -> str:
    return format_decimal(to_decimal(value), decimals)


def format_optional_raw(value: Any) -> str:
    if value is None:
        return "0"
    return str(value)


def compute_weighted_reward_apy(
    allocation_markets: Iterable[Dict[str, Any]], total_assets_usd: Decimal
) -> Decimal:
    if total_assets_usd <= 0:
        return Decimal("0")
    total = Decimal("0")
    for allocation in allocation_markets:
        market = safe_get(allocation, "market", {})
        market_state = safe_get(market, "state", {})
        supply_assets_usd = to_decimal(safe_get(allocation, "supplyAssetsUsd", 0))
        if supply_assets_usd <= 0:
            continue
        rewards = safe_get(market_state, "rewards", []) or []
        reward_apr_sum = Decimal("0")
        for reward in rewards:
            reward_apr_sum += to_decimal(safe_get(reward, "supplyApr", 0))
        weight = supply_assets_usd / total_assets_usd
        total += reward_apr_sum * weight
    return total


class MorphoClient:
    def __init__(self) -> None:
        self._url = settings.morpho_graphql_url
        self._client = httpx.AsyncClient(timeout=20)

    async def close(self) -> None:
        await self._client.aclose()

    async def _query(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self._client.post(self._url, json={"query": query, "variables": variables})
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise ValueError(f"GraphQL error: {data['errors']}")
        return data.get("data", {})

    async def fetch_user_by_address(self, chain_id: int, address: str) -> Dict[str, Any]:
        return await self._query(USER_BY_ADDRESS_QUERY, {"chainId": chain_id, "address": address})

    async def fetch_vault_by_address(self, chain_id: int, address: str) -> Dict[str, Any]:
        return await self._query(VAULT_BY_ADDRESS_QUERY, {"chainId": chain_id, "address": address})

    async def fetch_markets(self, chain_id: int) -> Dict[str, Any]:
        return await self._query(MARKETS_QUERY, {"chainIds": [chain_id]})


async def build_vault_position_from_v1(position: Dict[str, Any]) -> Dict[str, Any]:
    state = safe_get(position, "state", {})
    vault = safe_get(position, "vault", {})
    vault_state = safe_get(vault, "state", {})
    asset = safe_get(vault, "asset", {})

    total_assets_usd = to_decimal(safe_get(vault_state, "totalAssetsUsd", 0))
    allocation = safe_get(vault_state, "allocation", []) or []

    reward_apy_dec = compute_weighted_reward_apy(allocation, total_assets_usd)
    net_apy_dec = to_decimal(safe_get(vault_state, "avgNetApy", 0))
    base_apy_dec = max(net_apy_dec - reward_apy_dec, Decimal("0"))

    allocations = []
    for item in allocation:
        market = safe_get(item, "market", {})
        loan = safe_get(market, "loanAsset", {})
        collateral = safe_get(market, "collateralAsset", {})
        market_state = safe_get(market, "state", {})
        supply_assets_usd = to_decimal(safe_get(item, "supplyAssetsUsd", 0))
        allocation_percent = (
            supply_assets_usd / total_assets_usd * Decimal("100")
            if total_assets_usd > 0
            else Decimal("0")
        )
        market_name = f"{safe_get(collateral, 'symbol', 'N/A')}/{safe_get(loan, 'symbol', 'N/A')}"
        allocations.append(
            {
                "marketId": safe_get(market, "uniqueKey"),
                "marketName": market_name,
                "allocationPercent": format_decimal(allocation_percent, 2),
                "supplyApy": to_percent(safe_get(market_state, "avgNetSupplyApy", 0), 2),
            }
        )
    allocations.sort(key=lambda x: to_decimal(x.get("allocationPercent", 0)), reverse=True)

    curator_name = None
    curators = safe_get(vault_state, "curators", []) or []
    if curators:
        curator_name = safe_get(curators[0], "name")

    return {
        "vaultAddress": safe_get(vault, "address"),
        "vaultName": safe_get(vault, "name"),
        "curator": curator_name,
        "asset": safe_get(asset, "symbol"),
        "assetAddress": safe_get(asset, "address"),
        "shares": format_optional_raw(safe_get(state, "shares")),
        "balance": format_optional_decimal(safe_get(state, "assets"), 2),
        "balanceUsd": format_optional_decimal(safe_get(state, "assetsUsd"), 2),
        "apy": {
            "netApy": to_percent(net_apy_dec, 2),
            "baseApy": to_percent(base_apy_dec, 2),
            "rewardApy": to_percent(reward_apy_dec, 2),
        },
        "performanceFee": format_optional_decimal(safe_get(vault_state, "fee"), 2),
        "totalAssets": format_optional_decimal(total_assets_usd, 2),
        "allocations": allocations,
    }


async def build_vault_position_from_v2(position: Dict[str, Any], allocation_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    vault = safe_get(position, "vault", {})
    asset = safe_get(vault, "asset", {})

    total_assets_usd = to_decimal(safe_get(vault, "totalAssetsUsd", 0))
    rewards = safe_get(vault, "rewards", []) or []
    reward_apy_dec = sum((to_decimal(safe_get(r, "supplyApr", 0)) for r in rewards), Decimal("0"))
    net_apy_dec = to_decimal(safe_get(vault, "avgNetApy", 0))
    base_apy_dec = max(net_apy_dec - reward_apy_dec, Decimal("0"))

    allocations = []
    if allocation_data:
        allocation_state = safe_get(allocation_data, "state", {})
        allocation = safe_get(allocation_state, "allocation", []) or []
        total_assets_usd = to_decimal(safe_get(allocation_state, "totalAssetsUsd", total_assets_usd))
        for item in allocation:
            market = safe_get(item, "market", {})
            loan = safe_get(market, "loanAsset", {})
            collateral = safe_get(market, "collateralAsset", {})
            market_state = safe_get(market, "state", {})
            supply_assets_usd = to_decimal(safe_get(item, "supplyAssetsUsd", 0))
            allocation_percent = (
                supply_assets_usd / total_assets_usd * Decimal("100")
                if total_assets_usd > 0
                else Decimal("0")
            )
            market_name = f"{safe_get(collateral, 'symbol', 'N/A')}/{safe_get(loan, 'symbol', 'N/A')}"
            allocations.append(
                {
                    "marketId": safe_get(market, "uniqueKey"),
                    "marketName": market_name,
                    "allocationPercent": format_decimal(allocation_percent, 2),
                    "supplyApy": to_percent(safe_get(market_state, "avgNetSupplyApy", 0), 2),
                }
            )
        allocations.sort(key=lambda x: to_decimal(x.get("allocationPercent", 0)), reverse=True)

    curator_name = None
    curators = safe_get(vault, "curators", {})
    if isinstance(curators, dict):
        cur_items = safe_get(curators, "items", []) or []
        if cur_items:
            curator_name = safe_get(cur_items[0], "name")

    return {
        "vaultAddress": safe_get(vault, "address"),
        "vaultName": safe_get(vault, "name"),
        "curator": curator_name,
        "asset": safe_get(asset, "symbol"),
        "assetAddress": safe_get(asset, "address"),
        "shares": format_optional_raw(safe_get(position, "shares")),
        "balance": format_optional_decimal(safe_get(position, "assets"), 2),
        "balanceUsd": format_optional_decimal(safe_get(position, "assetsUsd"), 2),
        "apy": {
            "netApy": to_percent(net_apy_dec, 2),
            "baseApy": to_percent(base_apy_dec, 2),
            "rewardApy": to_percent(reward_apy_dec, 2),
        },
        "performanceFee": format_optional_decimal(safe_get(vault, "performanceFee"), 2),
        "totalAssets": format_optional_decimal(total_assets_usd, 2),
        "allocations": allocations,
    }


def build_market_positions(market_positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for position in market_positions:
        state = safe_get(position, "state", {})
        market = safe_get(position, "market", {})
        loan = safe_get(market, "loanAsset", {})
        collateral = safe_get(market, "collateralAsset", {})
        market_state = safe_get(market, "state", {})

        items.append(
            {
                "marketId": safe_get(market, "uniqueKey"),
                "loanAsset": safe_get(loan, "symbol"),
                "loanAssetAddress": safe_get(loan, "address"),
                "collateralAsset": safe_get(collateral, "symbol"),
                "collateralAssetAddress": safe_get(collateral, "address"),
                "oracle": safe_get(safe_get(market, "oracle", {}), "address"),
                "irm": safe_get(market, "irmAddress"),
                "lltv": normalize_lltv(safe_get(market, "lltv"), 2),
                "supply": {
                    "shares": format_optional_raw(safe_get(state, "supplyShares")),
                    "assets": format_optional_decimal(safe_get(state, "supplyAssets"), 2),
                    "assetsUsd": format_optional_decimal(safe_get(state, "supplyAssetsUsd"), 2),
                },
                "borrow": {
                    "shares": format_optional_raw(safe_get(state, "borrowShares")),
                    "assets": format_optional_decimal(safe_get(state, "borrowAssets"), 2),
                    "assetsUsd": format_optional_decimal(safe_get(state, "borrowAssetsUsd"), 2),
                },
                "collateral": {
                    "assets": format_optional_decimal(safe_get(state, "collateral"), 2),
                    "assetsUsd": format_optional_decimal(safe_get(state, "collateralUsd"), 2),
                },
                "healthFactor": format_optional_decimal(safe_get(position, "healthFactor"), 2),
                "apy": {
                    "borrowApy": to_percent(safe_get(market_state, "borrowApy", 0), 2),
                    "avgBorrowApy": to_percent(safe_get(market_state, "avgBorrowApy", 0), 2),
                    "netBorrowApy": to_percent(safe_get(market_state, "avgNetBorrowApy", 0), 2),
                },
                "_extra": {
                    "priceVariationToLiquidationPrice": safe_get(
                        position, "priceVariationToLiquidationPrice"
                    ),
                    "loanPriceUsd": safe_get(loan, "priceUsd"),
                    "collateralPriceUsd": safe_get(collateral, "priceUsd"),
                },
            }
        )
    return items


def build_markets_response(data: Dict[str, Any]) -> Dict[str, Any]:
    vaults = safe_get(data, "vaults", {}).get("items", []) or []
    vault_v2s = safe_get(data, "vaultV2s", {}).get("items", []) or []
    markets = safe_get(data, "markets", {}).get("items", []) or []

    vault_items: List[Dict[str, Any]] = []

    for vault in vaults:
        asset = safe_get(vault, "asset", {})
        state = safe_get(vault, "state", {})
        curators = safe_get(state, "curators", []) or []
        curator_name = safe_get(curators[0], "name") if curators else None
        rewards = safe_get(state, "rewards", []) or []
        reward_apy_dec = sum((to_decimal(safe_get(r, "supplyApr", 0)) for r in rewards), Decimal("0"))
        net_apy_dec = to_decimal(safe_get(state, "avgNetApy", 0))
        base_apy_dec = max(to_decimal(safe_get(state, "avgApy", 0)) , Decimal("0"))

        vault_items.append(
            {
                "vaultAddress": safe_get(vault, "address"),
                "vaultName": safe_get(vault, "name"),
                "curator": curator_name,
                "asset": safe_get(asset, "symbol"),
                "assetAddress": safe_get(asset, "address"),
                "totalAssets": format_optional_decimal(safe_get(state, "totalAssets"), 2),
                "totalAssetsUsd": format_optional_decimal(safe_get(state, "totalAssetsUsd"), 2),
                "apy": {
                    "netApy": to_percent(net_apy_dec, 2),
                    "baseApy": to_percent(base_apy_dec, 2),
                    "rewardApy": to_percent(reward_apy_dec, 2),
                },
                "performanceFee": format_optional_decimal(safe_get(state, "fee"), 2),
            }
        )

    for vault in vault_v2s:
        asset = safe_get(vault, "asset", {})
        curators = safe_get(vault, "curators", {})
        cur_items = safe_get(curators, "items", []) or []
        curator_name = safe_get(cur_items[0], "name") if cur_items else None
        rewards = safe_get(vault, "rewards", []) or []
        reward_apy_dec = sum((to_decimal(safe_get(r, "supplyApr", 0)) for r in rewards), Decimal("0"))
        net_apy_dec = to_decimal(safe_get(vault, "avgNetApy", 0))
        base_apy_dec = max(to_decimal(safe_get(vault, "avgApy", 0)), Decimal("0"))

        vault_items.append(
            {
                "vaultAddress": safe_get(vault, "address"),
                "vaultName": safe_get(vault, "name"),
                "curator": curator_name,
                "asset": safe_get(asset, "symbol"),
                "assetAddress": safe_get(asset, "address"),
                "totalAssets": format_optional_decimal(safe_get(vault, "totalAssets"), 2),
                "totalAssetsUsd": format_optional_decimal(safe_get(vault, "totalAssetsUsd"), 2),
                "apy": {
                    "netApy": to_percent(net_apy_dec, 2),
                    "baseApy": to_percent(base_apy_dec, 2),
                    "rewardApy": to_percent(reward_apy_dec, 2),
                },
                "performanceFee": format_optional_decimal(safe_get(vault, "performanceFee"), 2),
            }
        )

    vault_items.sort(key=lambda x: to_decimal(x.get("totalAssetsUsd", 0)), reverse=True)

    market_items: List[Dict[str, Any]] = []
    for market in markets:
        loan = safe_get(market, "loanAsset", {})
        collateral = safe_get(market, "collateralAsset", {})
        state = safe_get(market, "state", {})
        market_items.append(
            {
                "marketId": safe_get(market, "uniqueKey"),
                "loanAsset": safe_get(loan, "symbol"),
                "loanAssetAddress": safe_get(loan, "address"),
                "collateralAsset": safe_get(collateral, "symbol"),
                "collateralAssetAddress": safe_get(collateral, "address"),
                "lltv": normalize_lltv(safe_get(market, "lltv"), 2),
                "totalSupply": format_optional_decimal(safe_get(state, "supplyAssets"), 2),
                "totalSupplyUsd": format_optional_decimal(safe_get(state, "supplyAssetsUsd"), 2),
                "totalBorrow": format_optional_decimal(safe_get(state, "borrowAssets"), 2),
                "totalBorrowUsd": format_optional_decimal(safe_get(state, "borrowAssetsUsd"), 2),
                "utilizationRate": format_optional_decimal(safe_get(state, "utilization"), 2),
                "supplyApy": to_percent(safe_get(state, "avgNetSupplyApy", 0), 2),
                "borrowApy": to_percent(safe_get(state, "avgNetBorrowApy", 0), 2),
            }
        )

    return {
        "vaults": vault_items,
        "markets": market_items,
    }
