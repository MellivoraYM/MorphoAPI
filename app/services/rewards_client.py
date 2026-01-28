from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import settings
from app.services.morpho_client import safe_get, to_decimal, format_decimal


class RewardsClient:
    def __init__(self) -> None:
        self._base_url = settings.rewards_base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=20)

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_user_rewards(self, address: str) -> List[Dict[str, Any]]:
        url = f"{self._base_url}/users/{address}/rewards"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            for key in ("data", "items", "rewards"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        if isinstance(data, list):
            return data
        return []

    async def fetch_assets_metadata(
        self, rewards: List[Dict[str, Any]]
    ) -> Dict[Tuple[str, int], Dict[str, Any]]:
        assets_by_chain: Dict[int, List[str]] = {}
        for reward in rewards:
            asset = safe_get(reward, "asset", default={}) or {}
            address = safe_get(asset, "address")
            chain_id = safe_get(asset, "chain_id")
            if not address or not chain_id:
                continue
            assets_by_chain.setdefault(int(chain_id), [])
            if address not in assets_by_chain[int(chain_id)]:
                assets_by_chain[int(chain_id)].append(address)

        if not assets_by_chain:
            return {}

        results: Dict[Tuple[str, int], Dict[str, Any]] = {}
        query = """
        query GetAssetsWithPrice($where: AssetsFilters) {
          assets(where: $where) {
            items {
              address
              name
              priceUsd
              chain {
                id
              }
            }
          }
        }
        """

        for chain_id, addresses in assets_by_chain.items():
            variables = {"where": {"address_in": addresses, "chainId_in": [chain_id]}}
            try:
                resp = await self._client.post(
                    settings.morpho_graphql_url, json={"query": query, "variables": variables}
                )
                resp.raise_for_status()
                payload = resp.json()
                items = (
                    safe_get(safe_get(payload, "data", {}), "assets", {})
                    .get("items", [])
                    or []
                )
                for item in items:
                    addr = safe_get(item, "address")
                    cid = safe_get(safe_get(item, "chain", {}), "id")
                    if addr and cid is not None:
                        results[(addr, int(cid))] = item
            except Exception:
                continue

        return results

    @staticmethod
    def _sum_claimable(reward: Dict[str, Any]) -> int:
        reward_type = safe_get(reward, "type")
        total_claimable_wei = 0

        if reward_type == "market-reward":
            for part_key in ("for_supply", "for_borrow", "for_collateral"):
                part = safe_get(reward, part_key, default=None)
                if not part:
                    continue
                total_claimable_wei += int(safe_get(part, "claimable_now", 0) or 0)
                total_claimable_wei += int(safe_get(part, "claimable_next", 0) or 0)
        elif reward_type == "uniform-reward":
            amount = safe_get(reward, "amount", default={})
            total_claimable_wei += int(safe_get(amount, "claimable_now", 0) or 0)
            total_claimable_wei += int(safe_get(amount, "claimable_next", 0) or 0)
        else:
            amount = safe_get(reward, "amount", default={})
            total_claimable_wei += int(safe_get(amount, "claimable_now", 0) or 0)
            total_claimable_wei += int(safe_get(amount, "claimable_next", 0) or 0)

        return total_claimable_wei

    async def build_unclaimed_rewards(self, rewards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        metadata = await self.fetch_assets_metadata(rewards)
        results: List[Dict[str, Any]] = []
        for reward in rewards:
            total_claimable_wei = self._sum_claimable(reward)
            if total_claimable_wei == 0:
                continue
            asset = safe_get(reward, "asset", default={}) or {}
            address = safe_get(asset, "address")
            chain_id = safe_get(asset, "chain_id")
            meta = metadata.get((address, int(chain_id))) if address and chain_id else {}
            decimals = int(safe_get(meta, "decimals", 18) or 18)
            symbol = safe_get(meta, "symbol") or safe_get(meta, "name")
            price_usd = safe_get(meta, "priceUsd")

            amount = Decimal(total_claimable_wei) / (Decimal(10) ** decimals)
            amount_usd = None
            if price_usd is not None:
                amount_usd = amount * to_decimal(price_usd)

            results.append(
                {
                    "rewardToken": symbol,
                    "rewardTokenAddress": address,
                    "amount": format_decimal(amount, 6),
                    "amountUsd": format_decimal(amount_usd, 2)
                    if amount_usd is not None
                    else None,
                    "source": safe_get(reward, "type"),
                }
            )

        return results
