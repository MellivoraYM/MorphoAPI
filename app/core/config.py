from __future__ import annotations

from typing import Dict, Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class ChainConfig(BaseSettings):
    chain_id: int
    rpc_url: str


class Settings(BaseSettings):
    morpho_graphql_url: str = Field(
        default="https://api.morpho.org/graphql", alias="MORPHO_GRAPHQL_URL"
    )
    rewards_base_url: str = Field(
        default="https://rewards.morpho.org/v1", alias="MORPHO_REWARDS_URL"
    )

    eth_rpc_url: str = Field(default="wss://ethereum-rpc.publicnode.com", alias="ETH_RPC_URL")
    arb_rpc_url: str = Field(
        default="https://public-arb-mainnet.fastnode.io", alias="ARB_RPC_URL"
    )
    base_rpc_url: str = Field(default="https://base.drpc.org", alias="BASE_RPC_URL")

    mongo_enabled: bool = Field(default=True, alias="MONGO_ENABLED")
    mongo_uri: str = Field(default="mongodb://localhost:27017", alias="MONGO_URI")
    mongo_db: str = Field(default="morpho", alias="MONGO_DB")

    class Config:
        env_file = ".env"
        extra = "ignore"

    def chain_configs(self) -> Dict[int, ChainConfig]:
        return {
            1: ChainConfig(chain_id=1, rpc_url=self.eth_rpc_url),
            42161: ChainConfig(chain_id=42161, rpc_url=self.arb_rpc_url),
            8453: ChainConfig(chain_id=8453, rpc_url=self.base_rpc_url),
        }


settings = Settings()
