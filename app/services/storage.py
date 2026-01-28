from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MongoStorage:
    def __init__(self) -> None:
        self._client: Optional[AsyncIOMotorClient] = None
        self._db = None

    def _get_client(self) -> Optional[AsyncIOMotorClient]:
        if not settings.mongo_enabled:
            return None
        if self._client is None:
            self._client = AsyncIOMotorClient(settings.mongo_uri)
            self._db = self._client[settings.mongo_db]
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()

    async def save_snapshot(self, collection: str, payload: Dict[str, Any]) -> None:
        if not settings.mongo_enabled:
            return
        self._get_client()
        if self._db is None:
            return
        doc = {"createdAt": _now_iso(), **payload}
        try:
            await self._db[collection].insert_one(doc)
        except Exception:
            pass

    def save_snapshot_background(self, collection: str, payload: Dict[str, Any]) -> None:
        if not settings.mongo_enabled:
            return
        asyncio.create_task(self.save_snapshot(collection, payload))
