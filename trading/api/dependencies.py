"""
Shared application state and dependency injection helpers.

The MatchingEngine and asyncio.Queue are created once at startup
and injected into route handlers via FastAPI's Depends() mechanism.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from trading.engine.matcher import MatchingEngine
from trading.persistence.event_log import EventLog
from trading.persistence.snapshot import SnapshotManager
from trading.risk.checker import RiskChecker

# Supported tickers — fixed set for this project
SUPPORTED_TICKERS = ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"]

# Module-level singletons initialized at startup
_engine: MatchingEngine | None = None
_order_queues: dict[str, asyncio.Queue] | None = None
_risk: RiskChecker | None = None
_event_log: EventLog | None = None
_snapshot_manager: SnapshotManager | None = None
_idempotency_store: "IdempotencyStore | None" = None


class IdempotencyStore:
    """
    In-memory store for idempotent order submission with TTL expiry.

    Maps client-supplied order_id → the OrderResponse dict returned on first
    submission. Duplicate submissions with the same order_id return the cached
    response with HTTP 200 (not 201) to signal it is a replay, not a new order.

    Entries expire after ttl_hours (default 24h). Expired entries are dropped
    lazily on get() and can be restored from the event log on restart via
    restore().
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}
        self._expires_at: dict[str, datetime] = {}

    def get(self, order_id: str) -> dict | None:
        expiry = self._expires_at.get(order_id)
        if expiry is not None and datetime.now(tz=timezone.utc) > expiry:
            del self._cache[order_id]
            del self._expires_at[order_id]
            return None
        return self._cache.get(order_id)

    def store(self, order_id: str, response: dict, ttl_hours: int = 24) -> None:
        self._cache[order_id] = response
        self._expires_at[order_id] = datetime.now(tz=timezone.utc) + timedelta(
            hours=ttl_hours
        )

    def restore(self, order_id: str, response: dict, expires_at: str) -> None:
        """
        Restore a cache entry from the event log during startup replay.

        Silently drops the entry if it has already expired so the cache
        does not accumulate stale entries across restarts.
        """
        expiry = datetime.fromisoformat(expires_at)
        if datetime.now(tz=timezone.utc) < expiry:
            self._cache[order_id] = response
            self._expires_at[order_id] = expiry

    def __contains__(self, order_id: str) -> bool:
        return self.get(order_id) is not None


def get_engine() -> MatchingEngine:
    """Return the shared MatchingEngine instance."""
    if _engine is None:
        raise RuntimeError("Engine not initialized. Call init_app_state() first.")
    return _engine


def get_order_queues() -> dict[str, asyncio.Queue]:
    """Return the per-ticker order queues."""
    if _order_queues is None:
        raise RuntimeError("Queues not initialized. Call init_app_state() first.")
    return _order_queues


def get_risk() -> RiskChecker:
    """Return the shared RiskChecker instance."""
    if _risk is None:
        raise RuntimeError("RiskChecker not initialized. Call init_app_state() first.")
    return _risk


def get_event_log() -> EventLog:
    """Return the shared EventLog instance."""
    if _event_log is None:
        raise RuntimeError("EventLog not initialized. Call init_app_state() first.")
    return _event_log


def get_snapshot_manager() -> SnapshotManager:
    """Return the shared SnapshotManager instance."""
    if _snapshot_manager is None:
        raise RuntimeError(
            "SnapshotManager not initialized. Call init_app_state() first."
        )
    return _snapshot_manager


def get_idempotency_store() -> IdempotencyStore:
    """Return the shared IdempotencyStore instance."""
    if _idempotency_store is None:
        raise RuntimeError(
            "IdempotencyStore not initialized. Call init_app_state() first."
        )
    return _idempotency_store


def init_app_state() -> (
    tuple[MatchingEngine, dict[str, asyncio.Queue], RiskChecker, EventLog, SnapshotManager]
):
    """
    Initialize shared application state.

    Called once during FastAPI lifespan startup.
    Returns (engine, queues, risk, event_log, snapshot_manager) for use in main.py.
    """
    global _engine, _order_queues, _risk, _event_log, _snapshot_manager, _idempotency_store
    _engine = MatchingEngine(SUPPORTED_TICKERS)
    _order_queues = {ticker: asyncio.Queue() for ticker in SUPPORTED_TICKERS}
    _risk = RiskChecker()
    _event_log = EventLog()
    _snapshot_manager = SnapshotManager()
    _idempotency_store = IdempotencyStore()
    return _engine, _order_queues, _risk, _event_log, _snapshot_manager
