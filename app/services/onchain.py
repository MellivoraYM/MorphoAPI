from __future__ import annotations

import asyncio
from typing import Dict, Optional

from web3 import HTTPProvider, Web3, WebsocketProvider

from app.core.config import settings

METAMORPHO_ADAPTER_ABI = [
    {
        "inputs": [],
        "name": "morphoVaultV1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]


class OnchainClient:
    def __init__(self) -> None:
        self._providers: Dict[int, Web3] = {}

    def _get_web3(self, chain_id: int) -> Web3:
        if chain_id in self._providers:
            return self._providers[chain_id]
        chain_configs = settings.chain_configs()
        if chain_id not in chain_configs:
            raise ValueError(f"Unsupported chainId {chain_id}")
        rpc_url = chain_configs[chain_id].rpc_url
        if rpc_url.startswith("wss"):
            provider = WebsocketProvider(rpc_url)
        else:
            provider = HTTPProvider(rpc_url)
        web3 = Web3(provider)
        self._providers[chain_id] = web3
        return web3

    def _read_morpho_vault_v1(self, chain_id: int, adapter_address: str) -> Optional[str]:
        web3 = self._get_web3(chain_id)
        if not web3.is_address(adapter_address):
            return None
        contract = web3.eth.contract(
            address=web3.to_checksum_address(adapter_address), abi=METAMORPHO_ADAPTER_ABI
        )
        try:
            return contract.functions.morphoVaultV1().call()
        except Exception:
            return None

    async def fetch_morpho_vault_v1(self, chain_id: int, adapter_address: str) -> Optional[str]:
        return await asyncio.to_thread(self._read_morpho_vault_v1, chain_id, adapter_address)
