"""
Shared application state and dependency injection helpers.

The MatchingEngine and asyncio.Queue are created once at startup
and injected into route handlers via FastAPI's Depends() mechanism.
"""

import asyncio
from trading.engine.matcher import MatchingEngine
from trading.risk.checker import RiskChecker

# Supported tickers — fixed set for this project
SUPPORTED_TICKERS = ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"]

# Module-level singletons initialized at startup
_engine: MatchingEngine | None = None
_order_queue: asyncio.Queue | None = None
_risk: RiskChecker | None = None


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


def init_app_state() -> tuple[MatchingEngine, asyncio.Queue, RiskChecker]:
    """
    Initialize shared application state.

    Called once during FastAPI lifespan startup.
    Returns (engine, queue, risk) for use in the consumer task.
    """
    global _engine, _order_queue, _risk
    _engine = MatchingEngine(SUPPORTED_TICKERS)
    _order_queue = asyncio.Queue()
    _risk = RiskChecker()
    return _engine, _order_queue, _risk
