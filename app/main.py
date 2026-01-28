from __future__ import annotations

import warnings

from fastapi import FastAPI

from app.api.routes.morpho import morpho_client, rewards_client, router as morpho_router, storage

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


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await morpho_client.close()
    await rewards_client.close()
    await storage.close()
