from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import JSON, BigInteger, Column, DateTime, Integer, String, UniqueConstraint, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.core.config import settings


Base = declarative_base()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


class RegisteredAddress(Base):
    __tablename__ = "registered_addresses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    protocol = Column(String(32), nullable=False)
    address = Column(String(64), nullable=False)
    chain_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_now_dt, nullable=False)

    __table_args__ = (
        UniqueConstraint("protocol", "address", "chain_id", name="uq_registered_address"),
    )


class PositionsSnapshot(Base):
    __tablename__ = "positions_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String(64), nullable=False)
    chain_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=_now_dt, nullable=False)
    payload = Column(JSON, nullable=False)


class LiquidationSnapshot(Base):
    __tablename__ = "liquidation_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String(64), nullable=False)
    chain_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=_now_dt, nullable=False)
    payload = Column(JSON, nullable=False)


class MarketsSnapshot(Base):
    __tablename__ = "markets_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chain_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=_now_dt, nullable=False)
    payload = Column(JSON, nullable=False)


class VaultTransaction(Base):
    __tablename__ = "vault_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String(64), nullable=False)
    chain_id = Column(Integer, nullable=False)
    type = Column(String(64), nullable=False)
    tx_hash = Column(String(128), nullable=False)
    timestamp = Column(BigInteger, nullable=False)
    block_number = Column(BigInteger, nullable=True)
    data = Column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint("address", "chain_id", "tx_hash", "type", name="uq_vault_tx"),
    )


class MarketTransaction(Base):
    __tablename__ = "market_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String(64), nullable=False)
    chain_id = Column(Integer, nullable=False)
    type = Column(String(64), nullable=False)
    tx_hash = Column(String(128), nullable=False)
    timestamp = Column(BigInteger, nullable=False)
    block_number = Column(BigInteger, nullable=True)
    data = Column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint("address", "chain_id", "tx_hash", "type", name="uq_market_tx"),
    )


class PositionsHistory(Base):
    __tablename__ = "positions_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String(64), nullable=False)
    chain_id = Column(Integer, nullable=False)
    timestamp = Column(BigInteger, nullable=False)
    payload = Column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint("address", "chain_id", "timestamp", name="uq_positions_history"),
    )


class MySQLStorage:
    def __init__(self) -> None:
        self._engine = create_engine(
            settings.mysql_url,
            pool_pre_ping=True,
            pool_recycle=3600,
            future=True,
        )
        self._session_factory = sessionmaker(self._engine, expire_on_commit=False, class_=Session)
        Base.metadata.create_all(self._engine)

    @contextlib.contextmanager
    def session(self) -> Iterable[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self) -> None:
        self._engine.dispose()

    def register_addresses(
        self, protocol: str, addresses: Sequence[str], chain_ids: Optional[Sequence[int]] = None
    ) -> tuple[List[str], List[str]]:
        if not addresses:
            return [], []
        normalized = [addr.lower() for addr in addresses]
        with self.session() as session:
            if chain_ids:
                existing = (
                    session.query(RegisteredAddress.address)
                    .filter(
                        RegisteredAddress.protocol == protocol,
                        RegisteredAddress.chain_id.in_(chain_ids),
                        RegisteredAddress.address.in_(normalized),
                    )
                    .all()
                )
            else:
                existing = (
                    session.query(RegisteredAddress.address)
                    .filter(
                        RegisteredAddress.protocol == protocol,
                        RegisteredAddress.chain_id.is_(None),
                        RegisteredAddress.address.in_(normalized),
                    )
                    .all()
                )
            existing_set = {row[0] for row in existing}

            inserted: List[str] = []
            skipped: List[str] = []

            if chain_ids:
                items = []
                for addr in normalized:
                    if addr in existing_set:
                        skipped.append(addr)
                        continue
                    for chain_id in chain_ids:
                        items.append(RegisteredAddress(protocol=protocol, address=addr, chain_id=chain_id))
                    inserted.append(addr)
                session.add_all(items)
            else:
                items = []
                for addr in normalized:
                    if addr in existing_set:
                        skipped.append(addr)
                        continue
                    items.append(RegisteredAddress(protocol=protocol, address=addr, chain_id=None))
                    inserted.append(addr)
                session.add_all(items)

            try:
                session.flush()
            except IntegrityError:
                session.rollback()
            return inserted, skipped

    def list_registered(self, protocol: str) -> List[RegisteredAddress]:
        with self.session() as session:
            return (
                session.query(RegisteredAddress)
                .filter(RegisteredAddress.protocol == protocol)
                .all()
            )

    def save_positions_snapshot(self, payload: Dict[str, Any]) -> None:
        with self.session() as session:
            session.add(
                PositionsSnapshot(
                    address=payload.get("address", ""),
                    chain_id=payload.get("chainId", 0),
                    payload=payload,
                )
            )

    def save_liquidation_snapshot(self, payload: Dict[str, Any]) -> None:
        with self.session() as session:
            session.add(
                LiquidationSnapshot(
                    address=payload.get("address", ""),
                    chain_id=payload.get("chainId", 0),
                    payload=payload,
                )
            )

    def save_markets_snapshot(self, payload: Dict[str, Any]) -> None:
        with self.session() as session:
            session.add(
                MarketsSnapshot(
                    chain_id=payload.get("chainId", 0),
                    payload=payload,
                )
            )

    def save_vault_transaction(self, payload: Dict[str, Any]) -> None:
        with self.session() as session:
            session.add(
                VaultTransaction(
                    address=payload["address"],
                    chain_id=payload["chainId"],
                    type=payload["type"],
                    tx_hash=payload["txHash"],
                    timestamp=payload["timestamp"],
                    block_number=payload.get("blockNumber"),
                    data=payload.get("data", {}),
                )
            )
            try:
                session.flush()
            except IntegrityError:
                session.rollback()

    def save_market_transaction(self, payload: Dict[str, Any]) -> None:
        with self.session() as session:
            session.add(
                MarketTransaction(
                    address=payload["address"],
                    chain_id=payload["chainId"],
                    type=payload["type"],
                    tx_hash=payload["txHash"],
                    timestamp=payload["timestamp"],
                    block_number=payload.get("blockNumber"),
                    data=payload.get("data", {}),
                )
            )
            try:
                session.flush()
            except IntegrityError:
                session.rollback()

    def save_vault_transactions(self, items: Sequence[Dict[str, Any]]) -> None:
        if not items:
            return
        with self.session() as session:
            for payload in items:
                session.add(
                    VaultTransaction(
                        address=payload["address"],
                        chain_id=payload["chainId"],
                        type=payload["type"],
                        tx_hash=payload["txHash"],
                        timestamp=payload["timestamp"],
                        block_number=payload.get("blockNumber"),
                        data=payload.get("data", {}),
                    )
                )
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()

    def save_market_transactions(self, items: Sequence[Dict[str, Any]]) -> None:
        if not items:
            return
        with self.session() as session:
            for payload in items:
                session.add(
                    MarketTransaction(
                        address=payload["address"],
                        chain_id=payload["chainId"],
                        type=payload["type"],
                        tx_hash=payload["txHash"],
                        timestamp=payload["timestamp"],
                        block_number=payload.get("blockNumber"),
                        data=payload.get("data", {}),
                    )
                )
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()

    def save_positions_history(self, payload: Dict[str, Any]) -> None:
        with self.session() as session:
            session.add(
                PositionsHistory(
                    address=payload["address"],
                    chain_id=payload["chainId"],
                    timestamp=payload["timestamp"],
                    payload=payload,
                )
            )
            try:
                session.flush()
            except IntegrityError:
                session.rollback()

    def fetch_vault_transactions(
        self, address: str, chain_id: int, limit: int = 100
    ) -> List[VaultTransaction]:
        with self.session() as session:
            return (
                session.query(VaultTransaction)
                .filter(VaultTransaction.address == address, VaultTransaction.chain_id == chain_id)
                .order_by(VaultTransaction.timestamp.desc())
                .limit(limit)
                .all()
            )

    def fetch_market_transactions(
        self, address: str, chain_id: int, limit: int = 100
    ) -> List[MarketTransaction]:
        with self.session() as session:
            return (
                session.query(MarketTransaction)
                .filter(MarketTransaction.address == address, MarketTransaction.chain_id == chain_id)
                .order_by(MarketTransaction.timestamp.desc())
                .limit(limit)
                .all()
            )

    def fetch_vault_transactions_by_time(
        self, address: str, chain_id: int, start_ts: int, end_ts: int
    ) -> List[VaultTransaction]:
        with self.session() as session:
            return (
                session.query(VaultTransaction)
                .filter(
                    VaultTransaction.address == address,
                    VaultTransaction.chain_id == chain_id,
                    VaultTransaction.timestamp >= start_ts,
                    VaultTransaction.timestamp <= end_ts,
                )
                .all()
            )

    def fetch_positions_snapshot_before(
        self, address: str, chain_id: int, ts: int
    ) -> Optional[PositionsSnapshot]:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        with self.session() as session:
            return (
                session.query(PositionsSnapshot)
                .filter(
                    PositionsSnapshot.address == address,
                    PositionsSnapshot.chain_id == chain_id,
                    PositionsSnapshot.created_at <= dt,
                )
                .order_by(PositionsSnapshot.created_at.desc())
                .first()
            )

    def fetch_positions_snapshot_at_or_after(
        self, address: str, chain_id: int, ts: int
    ) -> Optional[PositionsSnapshot]:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        with self.session() as session:
            return (
                session.query(PositionsSnapshot)
                .filter(
                    PositionsSnapshot.address == address,
                    PositionsSnapshot.chain_id == chain_id,
                    PositionsSnapshot.created_at >= dt,
                )
                .order_by(PositionsSnapshot.created_at.asc())
                .first()
            )

    def fetch_positions_history(
        self, address: str, chain_id: int, start_ts: int, end_ts: int
    ) -> List[PositionsHistory]:
        with self.session() as session:
            return (
                session.query(PositionsHistory)
                .filter(
                    PositionsHistory.address == address,
                    PositionsHistory.chain_id == chain_id,
                    PositionsHistory.timestamp >= start_ts,
                    PositionsHistory.timestamp <= end_ts,
                )
                .order_by(PositionsHistory.timestamp.asc())
                .all()
            )
