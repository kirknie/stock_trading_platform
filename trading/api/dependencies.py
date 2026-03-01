"""
Shared application state and dependency injection helpers.

The MatchingEngine and asyncio.Queue are created once at startup
and injected into route handlers via FastAPI's Depends() mechanism.
"""

import asyncio
from trading.engine.matcher import MatchingEngine
from trading.persistence.event_log import EventLog
from trading.persistence.snapshot import SnapshotManager
from trading.risk.checker import RiskChecker

# Supported tickers — fixed set for this project
SUPPORTED_TICKERS = ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"]

# Module-level singletons initialized at startup
_engine: MatchingEngine | None = None
_order_queue: asyncio.Queue | None = None
_risk: RiskChecker | None = None
_event_log: EventLog | None = None
_snapshot_manager: SnapshotManager | None = None
_idempotency_store: "IdempotencyStore | None" = None


class IdempotencyStore:
    """
    In-memory store for idempotent order submission.

    Maps client-supplied order_id → the OrderResponse dict returned on first
    submission. Duplicate submissions with the same order_id return the cached
    response with HTTP 200 (not 201) to signal it is a replay, not a new order.

    Limitation: unbounded in-memory growth with no TTL. A production system
    would use a time-bounded backing store such as Redis.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    def get(self, order_id: str) -> dict | None:
        return self._cache.get(order_id)

    def store(self, order_id: str, response: dict) -> None:
        self._cache[order_id] = response

    def __contains__(self, order_id: str) -> bool:
        return order_id in self._cache


def get_engine() -> MatchingEngine:
    """Return the shared MatchingEngine instance."""
    if _engine is None:
        raise RuntimeError("Engine not initialized. Call init_app_state() first.")
    return _engine


def get_order_queue() -> asyncio.Queue:
    """Return the shared async order queue."""
    if _order_queue is None:
        raise RuntimeError("Queue not initialized. Call init_app_state() first.")
    return _order_queue


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
        raise RuntimeError("SnapshotManager not initialized. Call init_app_state() first.")
    return _snapshot_manager


def get_idempotency_store() -> IdempotencyStore:
    """Return the shared IdempotencyStore instance."""
    if _idempotency_store is None:
        raise RuntimeError("IdempotencyStore not initialized. Call init_app_state() first.")
    return _idempotency_store


def init_app_state() -> tuple[MatchingEngine, asyncio.Queue, RiskChecker, EventLog, SnapshotManager]:
    """
    Initialize shared application state.

    Called once during FastAPI lifespan startup.
    Returns (engine, queue, risk, event_log, snapshot_manager) for use in main.py.
    """
    global _engine, _order_queue, _risk, _event_log, _snapshot_manager, _idempotency_store
    _engine = MatchingEngine(SUPPORTED_TICKERS)
    _order_queue = asyncio.Queue()
    _risk = RiskChecker()
    _event_log = EventLog()
    _snapshot_manager = SnapshotManager()
    _idempotency_store = IdempotencyStore()
    return _engine, _order_queue, _risk, _event_log, _snapshot_manager
