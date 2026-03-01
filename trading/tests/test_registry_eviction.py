"""
Tests for order registry eviction in MatchingEngine.

Verifies that evict_stale_registry() removes terminal orders (FILLED,
CANCELED, REJECTED) older than the TTL while leaving open (resting)
orders untouched.

Pattern for each test:
  1. Submit orders to put the engine into the desired state
  2. Optionally backdate _registry_timestamps to simulate staleness
  3. Call evict_stale_registry() with a short TTL
  4. Assert registry contents match expectations
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading.engine.matcher import MatchingEngine
from trading.events.models import Order, OrderSide, OrderStatus, OrderType


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_engine() -> MatchingEngine:
    return MatchingEngine(["AAPL", "MSFT"])


def make_limit_order(
    order_id: str = "O-1",
    ticker: str = "AAPL",
    side: OrderSide = OrderSide.BUY,
    price: str = "150.00",
    quantity: int = 100,
) -> Order:
    return Order(
        order_id=order_id,
        ticker=ticker,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        price=Decimal(price),
        timestamp=datetime.now(tz=timezone.utc),
        account_id="acc1",
    )


def backdate(engine: MatchingEngine, order_id: str, seconds: int) -> None:
    """Move a registry timestamp back by `seconds` to simulate staleness."""
    engine._registry_timestamps[order_id] = datetime.now(tz=timezone.utc) - timedelta(
        seconds=seconds
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_fresh_terminal_order_not_evicted():
    """A just-filled order is terminal but within TTL — must not be evicted."""
    engine = make_engine()
    engine.submit_order(make_limit_order("O-sell", side=OrderSide.SELL, price="150.00"))
    engine.submit_order(make_limit_order("O-buy", side=OrderSide.BUY, price="150.00"))

    # Both orders are filled; timestamp is now — well within 1-hour TTL
    evicted = engine.evict_stale_registry(ttl_seconds=3600)

    assert evicted == 0
    # Filled orders remain in registry (not yet old enough)
    assert "O-sell" in engine.order_registry or "O-buy" in engine.order_registry


def test_stale_terminal_order_is_evicted():
    """A filled order with a timestamp older than TTL must be evicted."""
    engine = make_engine()
    engine.submit_order(make_limit_order("O-sell", side=OrderSide.SELL, price="150.00"))
    engine.submit_order(make_limit_order("O-buy", side=OrderSide.BUY, price="150.00"))

    # Backdate the filled sell order beyond 1 hour
    backdate(engine, "O-sell", seconds=3601)

    evicted = engine.evict_stale_registry(ttl_seconds=3600)

    assert evicted == 1
    assert "O-sell" not in engine.order_registry
    assert "O-sell" not in engine._registry_timestamps


def test_open_order_never_evicted():
    """A resting LIMIT order must not be evicted regardless of age."""
    engine = make_engine()
    engine.submit_order(make_limit_order("O-rest", side=OrderSide.BUY, price="100.00"))

    # Backdate far beyond any reasonable TTL
    backdate(engine, "O-rest", seconds=86400)

    evicted = engine.evict_stale_registry(ttl_seconds=3600)

    assert evicted == 0
    assert "O-rest" in engine.order_registry


def test_evict_returns_count():
    """evict_stale_registry() returns the number of entries removed."""
    engine = make_engine()

    # Submit two sell orders (makers) then match each with a buy (aggressor).
    # Only the makers enter the registry; aggressors that fully fill are never added.
    engine.submit_order(
        make_limit_order("O-s1", ticker="AAPL", side=OrderSide.SELL, price="150.00", quantity=100)
    )
    engine.submit_order(
        make_limit_order("O-b1", ticker="AAPL", side=OrderSide.BUY, price="150.00", quantity=100)
    )
    engine.submit_order(
        make_limit_order("O-s2", ticker="MSFT", side=OrderSide.SELL, price="300.00", quantity=50)
    )
    engine.submit_order(
        make_limit_order("O-b2", ticker="MSFT", side=OrderSide.BUY, price="300.00", quantity=50)
    )

    # Only the two sell (maker) orders are in the registry
    assert set(engine.order_registry) == {"O-s1", "O-s2"}

    # Backdate both terminal entries
    for order_id in list(engine._registry_timestamps):
        backdate(engine, order_id, seconds=3601)

    evicted = engine.evict_stale_registry(ttl_seconds=3600)

    assert evicted == 2
    assert engine.order_registry == {}
    assert engine._registry_timestamps == {}


def test_only_stale_terminal_orders_evicted_mixed_registry():
    """With a mix of fresh, stale, and open orders, only stale terminal ones go."""
    engine = make_engine()

    # Resting order at a price that won't match (open, never evicted)
    engine.submit_order(
        make_limit_order("O-rest", ticker="AAPL", side=OrderSide.BUY, price="100.00")
    )

    # Two sell orders (makers) that rest, then each gets matched by a buy (aggressor).
    # Both become FILLED (terminal) and stay in the registry.
    engine.submit_order(
        make_limit_order("O-sell1", ticker="AAPL", side=OrderSide.SELL, price="150.00")
    )
    engine.submit_order(
        make_limit_order("O-sell2", ticker="AAPL", side=OrderSide.SELL, price="160.00")
    )
    engine.submit_order(
        make_limit_order("O-buy1", ticker="AAPL", side=OrderSide.BUY, price="150.00")
    )
    engine.submit_order(
        make_limit_order("O-buy2", ticker="AAPL", side=OrderSide.BUY, price="160.00")
    )

    # Backdate only O-sell1 (stale); O-sell2 stays fresh
    backdate(engine, "O-sell1", seconds=3601)

    evicted = engine.evict_stale_registry(ttl_seconds=3600)

    assert evicted == 1
    assert "O-sell1" not in engine.order_registry  # stale terminal — evicted
    assert "O-rest" in engine.order_registry        # resting — kept
    assert "O-sell2" in engine.order_registry       # terminal but fresh — kept


def test_cancel_removes_from_registry_immediately():
    """Cancellation removes the entry immediately — eviction has nothing to do."""
    engine = make_engine()
    engine.submit_order(make_limit_order("O-cancel", side=OrderSide.BUY, price="100.00"))
    assert "O-cancel" in engine.order_registry

    engine.cancel_order("O-cancel")

    assert "O-cancel" not in engine.order_registry
    assert "O-cancel" not in engine._registry_timestamps

    # Eviction of an already-empty registry is a no-op
    evicted = engine.evict_stale_registry(ttl_seconds=0)
    assert evicted == 0


def test_evict_empty_registry_is_no_op():
    """Calling evict on an engine with no registered orders returns 0."""
    engine = make_engine()
    assert engine.evict_stale_registry(ttl_seconds=3600) == 0


def test_timestamps_cleared_on_eviction():
    """_registry_timestamps must not retain entries for evicted orders."""
    engine = make_engine()
    engine.submit_order(make_limit_order("O-sell", side=OrderSide.SELL, price="150.00"))
    engine.submit_order(make_limit_order("O-buy", side=OrderSide.BUY, price="150.00"))

    for order_id in list(engine._registry_timestamps):
        backdate(engine, order_id, seconds=7200)

    engine.evict_stale_registry(ttl_seconds=3600)

    assert engine._registry_timestamps == {}
