from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PositionsResponse(BaseModel):
    address: str
    protocol: str
    chainId: int
    timestamp: str
    summary: Dict[str, str]
    vaultPositions: List[Dict[str, Any]]
    marketPositions: List[Dict[str, Any]]
    rewards: Dict[str, Any]


class LiquidationResponse(BaseModel):
    address: str
    chainId: int
    timestamp: str
    marketPositions: List[Dict[str, Any]]


class MarketsResponse(BaseModel):
    chainId: int
    timestamp: str
    vaults: List[Dict[str, Any]]
    markets: List[Dict[str, Any]]
