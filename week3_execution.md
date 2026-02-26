# Week 3 Execution Plan: Risk, Persistence & Recovery

## Overview

Harden the trading platform with production-facing concerns:

**Goal:** By end of week, the platform can:
- Enforce pre-trade risk limits per account (position size, notional exposure)
- Protect market orders from wide-spread execution
- Persist every order, trade, and cancellation to an append-only event log
- Recover to identical in-memory state after a restart (snapshot + replay)
- Reject duplicate `order_id` submissions (idempotency)

No new external dependencies beyond `aiofiles` (async file I/O).

---

## Architecture After Week 3

```
HTTP Client
    |
    ↓
FastAPI route handler
    |
    | OrderRequest validated by Pydantic
    ↓
RiskChecker.check(order) ──→ HTTP 422 if limit exceeded
    |
    | passes
    ↓
IdempotencyStore.check(order_id) ──→ HTTP 409 if duplicate
    |
    | new order_id
    ↓
asyncio.Queue
    |
    | (Order, Future)
    ↓
run_consumer
    |
    | engine.submit_order(order)
    ↓
MatchingEngine → OrderBook
    |
    | List[Trade]
    ↓
future.set_result(trades)
    |
    ↓
RiskChecker.record_fill(trade)   ← update position state
EventLog.append(OrderEvent)      ← persist every event
broadcaster.notify_*(...)         ← push WebSocket updates
```

---

## Pre-Week Setup (15 minutes)

### Step 1: Install New Dependency

```bash
uv add aiofiles
uv add --dev types-aiofiles
```

- `aiofiles` — async file I/O for the event log writer
- `types-aiofiles` — mypy stubs

Verify:
```bash
python -c "import aiofiles; print('OK')"
```

### Step 2: Extend Project Structure

```bash
mkdir -p trading/risk trading/persistence
touch trading/risk/__init__.py
touch trading/risk/checker.py
touch trading/persistence/__init__.py
touch trading/persistence/event_log.py
touch trading/persistence/snapshot.py
touch trading/tests/test_risk.py
touch trading/tests/test_persistence.py
```

Verify:
```bash
ls trading/risk/ trading/persistence/
```

---

## Day 1: Pre-Trade Risk Checks (4 hours)

### Goal
Reject orders that would violate per-account position limits or portfolio notional
exposure before they reach the matching engine.

---

### Step 1.1: Design the Risk Model (30 min)

Two limits enforced per order submission:

**Limit 1 — Position limit per (account, ticker)**
- Each account may hold at most `MAX_POSITION_QUANTITY` shares of any single ticker
- Applies to the net position: existing filled quantity + new order quantity
- Checked at order submission time (optimistic — fills are not guaranteed)

**Limit 2 — Portfolio notional exposure per account**
- Total `quantity × price` across all open (unfilled) orders for an account must not
  exceed `MAX_NOTIONAL_EXPOSURE`
- Market orders use `Decimal("0")` as their price for exposure purposes (unknown fill
  price), so they contribute `0` to notional — this is intentional conservatism
  (market orders execute immediately and don't add to open exposure)
- Checked only for LIMIT orders

**Configuration** (hardcoded constants for now, extracted to config in a later week):
```python
MAX_POSITION_QUANTITY = 10_000   # shares per (account, ticker)
MAX_NOTIONAL_EXPOSURE = Decimal("1_000_000")  # $1M per account across open orders
```

**State tracked by RiskChecker:**
```python
# account_id → ticker → filled quantity (confirmed fills only)
positions: dict[str, dict[str, int]]

# account_id → set of (order_id, quantity, price) for open LIMIT orders
open_orders: dict[str, set[tuple[str, int, Decimal]]]
```

---

### Step 1.2: Implement RiskChecker (1.5 hours)
**File:** `trading/risk/checker.py`

```python
"""
Pre-trade risk checks.

Enforces two limits before an order reaches the matching engine:

1. Position limit: net filled quantity per (account, ticker) ≤ MAX_POSITION_QUANTITY
2. Notional exposure: total open LIMIT order value per account ≤ MAX_NOTIONAL_EXPOSURE

The checker is stateful: it tracks confirmed fills and open orders.
The consumer calls record_fill() and record_cancel() to keep state current.
"""

from decimal import Decimal
from dataclasses import dataclass, field

from trading.events.models import Order, OrderType, Trade

MAX_POSITION_QUANTITY: int = 10_000
MAX_NOTIONAL_EXPOSURE: Decimal = Decimal("1_000_000")


@dataclass
class RiskViolation(Exception):
    """Raised when an order violates a pre-trade risk limit."""
    message: str

    def __str__(self) -> str:
        return self.message


class RiskChecker:
    """
    Stateful pre-trade risk checker.

    One instance is shared for the lifetime of the application.
    All methods are synchronous and called from the single consumer
    coroutine, so no locking is needed.
    """

    def __init__(
        self,
        max_position: int = MAX_POSITION_QUANTITY,
        max_notional: Decimal = MAX_NOTIONAL_EXPOSURE,
    ) -> None:
        self._max_position = max_position
        self._max_notional = max_notional
        # account_id → ticker → confirmed net filled quantity
        self._positions: dict[str, dict[str, int]] = {}
        # account_id → {(order_id, quantity, price)} for open LIMIT orders
        self._open_orders: dict[str, set[tuple[str, int, Decimal]]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def check(self, order: Order) -> None:
        """
        Validate an order against all risk limits.

        Raises RiskViolation if any limit is breached.
        Called synchronously from the consumer before engine.submit_order().
        """
        self._check_position_limit(order)
        if order.order_type == OrderType.LIMIT:
            self._check_notional_exposure(order)

    def record_open_order(self, order: Order) -> None:
        """
        Register a LIMIT order as open (adds to notional exposure tracking).

        Called after check() passes and before submit_order().
        """
        if order.order_type != OrderType.LIMIT:
            return
        price = order.price if order.price is not None else Decimal("0")
        bucket = self._open_orders.setdefault(order.account_id, set())
        bucket.add((order.order_id, order.quantity, price))

    def record_fill(self, trade: Trade, buyer_account: str, seller_account: str) -> None:
        """
        Update position state after a confirmed trade.

        Called by the consumer once per trade, after engine.submit_order().
        """
        self._add_position(buyer_account, trade.ticker, trade.quantity)
        self._add_position(seller_account, trade.ticker, -trade.quantity)

    def record_cancel(self, order: Order) -> None:
        """
        Remove a cancelled LIMIT order from open exposure tracking.

        Called by the consumer or cancel route after a successful cancellation.
        """
        if order.order_type != OrderType.LIMIT:
            return
        price = order.price if order.price is not None else Decimal("0")
        bucket = self._open_orders.get(order.account_id, set())
        bucket.discard((order.order_id, order.quantity, price))

    def record_order_complete(self, order: Order) -> None:
        """
        Remove a fully filled order from open exposure tracking.

        Called by the consumer after all trades for an order are recorded.
        """
        self.record_cancel(order)  # same cleanup logic

    def get_position(self, account_id: str, ticker: str) -> int:
        """Return the current net filled position for an (account, ticker) pair."""
        return self._positions.get(account_id, {}).get(ticker, 0)

    def get_notional_exposure(self, account_id: str) -> Decimal:
        """Return the total notional value of open LIMIT orders for an account."""
        return sum(
            qty * price
            for _, qty, price in self._open_orders.get(account_id, set())
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _check_position_limit(self, order: Order) -> None:
        current = self.get_position(order.account_id, order.ticker)
        projected = current + order.quantity
        if projected > self._max_position:
            raise RiskViolation(
                f"Position limit exceeded for account '{order.account_id}' "
                f"on {order.ticker}: current={current}, order={order.quantity}, "
                f"projected={projected}, limit={self._max_position}"
            )

    def _check_notional_exposure(self, order: Order) -> None:
        price = order.price if order.price is not None else Decimal("0")
        current_exposure = self.get_notional_exposure(order.account_id)
        order_notional = Decimal(order.quantity) * price
        projected = current_exposure + order_notional
        if projected > self._max_notional:
            raise RiskViolation(
                f"Notional exposure limit exceeded for account '{order.account_id}': "
                f"current={current_exposure}, order={order_notional}, "
                f"projected={projected}, limit={self._max_notional}"
            )

    def _add_position(self, account_id: str, ticker: str, delta: int) -> None:
        account = self._positions.setdefault(account_id, {})
        account[ticker] = account.get(ticker, 0) + delta
```

**Validation:**
```bash
python -c "from trading.risk.checker import RiskChecker; print('OK')"
```

---

### Step 1.3: Wire RiskChecker Into the Consumer (45 min)

The consumer is the right place to run risk checks: it is single-threaded (no races),
and it is the gatekeeper before `engine.submit_order()`.

**File:** `trading/api/consumer.py` — add risk checking and state updates:

```python
"""
Async order consumer with risk checking and broadcaster integration.
"""

import asyncio
import logging

from trading.api.broadcaster import Broadcaster
from trading.engine.matcher import MatchingEngine
from trading.risk.checker import RiskChecker, RiskViolation

logger = logging.getLogger(__name__)


async def run_consumer(
    engine: MatchingEngine,
    queue: asyncio.Queue,
    broadcaster: Broadcaster,
    risk: RiskChecker,
) -> None:
    """
    Drain the order queue, enforce risk limits, process orders.

    Flow per order:
      1. risk.check(order)            → raises RiskViolation on breach
      2. risk.record_open_order(order) → register in exposure tracking
      3. engine.submit_order(order)    → matching engine
      4. risk.record_fill(trade, ...)  → update position state per trade
      5. risk.record_order_complete()  → remove from open exposure if fully filled
      6. broadcaster.notify_*()        → push WebSocket events
    """
    logger.info("Order consumer started")
    while True:
        try:
            order, future = await queue.get()
            try:
                risk.check(order)
                risk.record_open_order(order)

                trades = engine.submit_order(order)
                future.set_result(trades)

                # Update risk state from confirmed fills
                for trade in trades:
                    buyer_order = engine.order_registry.get(trade.buyer_order_id)
                    seller_order = engine.order_registry.get(trade.seller_order_id)
                    buyer_account = buyer_order[1].account_id if buyer_order else "unknown"
                    seller_account = seller_order[1].account_id if seller_order else "unknown"
                    risk.record_fill(trade, buyer_account, seller_account)

                if order.is_complete():
                    risk.record_order_complete(order)

                # Broadcast book and trade events
                book = engine.manager.get_order_book(order.ticker)
                await broadcaster.notify_book_update(
                    ticker=order.ticker,
                    best_bid=book.get_best_bid(),
                    best_ask=book.get_best_ask(),
                    spread=book.get_spread(),
                )
                for trade in trades:
                    await broadcaster.notify_trade(
                        trade_id=trade.trade_id,
                        ticker=trade.ticker,
                        price=trade.price,
                        quantity=trade.quantity,
                    )

            except RiskViolation as exc:
                future.set_exception(exc)
            except Exception as exc:
                future.set_exception(exc)
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            logger.info("Order consumer shutting down")
            break
        except Exception as exc:
            logger.error("Unexpected consumer error: %s", exc)
```

**Note on `engine.order_registry`:** The current `MatchingEngine` stores
`order_registry: dict[str, tuple[str, Order]]` (order_id → (ticker, order)).
We need to look up the buyer's and seller's account IDs from the trade's
order IDs. Verify the registry is accessible — see Step 1.4.

---

### Step 1.4: Expose order_registry on MatchingEngine (15 min)
**File:** `trading/engine/matcher.py`

The consumer needs to look up the account IDs for both sides of a trade.
`MatchingEngine._order_registry` is currently private (if named with underscore)
or may already be public. Check the current matcher.py and ensure it's accessible
as `engine.order_registry`.

Read `trading/engine/matcher.py` and verify the attribute name. If it's `_order_registry`,
add a public property:

```python
@property
def order_registry(self) -> dict[str, tuple[str, "Order"]]:
    return self._order_registry
```

If it's already `order_registry` (no underscore), no change needed.

---

### Step 1.5: Wire RiskChecker Into main.py and routes.py (30 min)

**File:** `trading/api/dependencies.py` — add risk singleton:

```python
# Add to existing module
from trading.risk.checker import RiskChecker

_risk: RiskChecker | None = None


def get_risk() -> RiskChecker:
    if _risk is None:
        raise RuntimeError("RiskChecker not initialized. Call init_app_state() first.")
    return _risk


def init_app_state() -> tuple[MatchingEngine, asyncio.Queue, RiskChecker]:
    global _engine, _order_queue, _risk
    _engine = MatchingEngine(SUPPORTED_TICKERS)
    _order_queue = asyncio.Queue()
    _risk = RiskChecker()
    return _engine, _order_queue, _risk
```

**File:** `main.py` — pass risk to consumer:

```python
engine, queue, risk = init_app_state()
broadcaster = init_broadcaster()

consumer_task = asyncio.create_task(
    consumer.run_consumer(engine, queue, broadcaster, risk),
    name="order-consumer"
)
```

**File:** `trading/api/routes.py` — handle `RiskViolation` from the future:

```python
from trading.risk.checker import RiskViolation

# In submit_order, after `trades = await future`:
try:
    trades = await future
except RiskViolation as exc:
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=str(exc),
    )
```

Also update `cancel_order` to record the cancellation with risk checker:

```python
from trading.api.dependencies import get_risk

@router.post("/orders/{order_id}/cancel", ...)
async def cancel_order(
    order_id: str,
    engine: MatchingEngine = Depends(get_engine),
    risk: RiskChecker = Depends(get_risk),
) -> CancelResponse:
    ticker_and_order = engine.order_registry.get(order_id)
    success = engine.cancel_order(order_id)
    if success and ticker_and_order is not None:
        _, order = ticker_and_order
        risk.record_cancel(order)
    return CancelResponse(
        order_id=order_id,
        success=success,
        message="Order canceled" if success else "Order not found or already completed",
    )
```

**Validation:**
```bash
python -c "from main import app; print('OK')"
pytest -q
```

---

### Step 1.6: Write Risk Tests (1 hour)
**File:** `trading/tests/test_risk.py`

Test the `RiskChecker` directly (no API layer) and also test that the API
returns 422 on violations.

```python
"""
Tests for pre-trade risk checks.
"""

import pytest
from decimal import Decimal
from datetime import datetime, timezone

from trading.risk.checker import RiskChecker, RiskViolation, MAX_POSITION_QUANTITY, MAX_NOTIONAL_EXPOSURE
from trading.events.models import Order, OrderSide, OrderType, Trade


def make_order(
    quantity: int,
    price: str | None = "100.00",
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.LIMIT,
    account_id: str = "acc1",
    ticker: str = "AAPL",
    order_id: str = "O-1",
) -> Order:
    return Order(
        order_id=order_id,
        ticker=ticker,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=Decimal(price) if price else None,
        timestamp=datetime.now(tz=timezone.utc),
        account_id=account_id,
    )


def make_trade(
    buyer_order_id: str = "O-buy",
    seller_order_id: str = "O-sell",
    quantity: int = 100,
    price: str = "100.00",
    ticker: str = "AAPL",
) -> Trade:
    return Trade(
        trade_id="T-1",
        ticker=ticker,
        buyer_order_id=buyer_order_id,
        seller_order_id=seller_order_id,
        price=Decimal(price),
        quantity=quantity,
        timestamp=datetime.now(tz=timezone.utc),
    )


# ── Position limit tests ──────────────────────────────────────────────────────

def test_position_limit_allows_order_within_limit():
    checker = RiskChecker(max_position=1000)
    order = make_order(quantity=500)
    checker.check(order)  # should not raise


def test_position_limit_rejects_order_exceeding_limit():
    checker = RiskChecker(max_position=1000)
    order = make_order(quantity=1001)
    with pytest.raises(RiskViolation, match="Position limit exceeded"):
        checker.check(order)


def test_position_limit_accumulates_across_fills():
    checker = RiskChecker(max_position=1000)
    trade = make_trade(buyer_order_id="O-1", quantity=700)
    checker.record_fill(trade, buyer_account="acc1", seller_account="acc2")

    # acc1 now has 700 shares — another 400 should breach
    order = make_order(quantity=400, account_id="acc1")
    with pytest.raises(RiskViolation, match="Position limit exceeded"):
        checker.check(order)


def test_position_limit_allows_after_sell_reduces_position():
    checker = RiskChecker(max_position=1000)
    # Buy 800
    buy_trade = make_trade(buyer_order_id="O-1", quantity=800)
    checker.record_fill(buy_trade, buyer_account="acc1", seller_account="acc2")

    # Sell 300 — position drops to 500
    sell_trade = make_trade(seller_order_id="O-2", quantity=300, buyer_order_id="O-3")
    checker.record_fill(sell_trade, buyer_account="acc3", seller_account="acc1")

    # Another 400 buy — 500 + 400 = 900 ≤ 1000, OK
    order = make_order(quantity=400, account_id="acc1")
    checker.check(order)  # should not raise


def test_position_limit_independent_per_ticker():
    checker = RiskChecker(max_position=1000)
    # Fill 900 on AAPL
    trade = make_trade(buyer_order_id="O-1", quantity=900, ticker="AAPL")
    checker.record_fill(trade, buyer_account="acc1", seller_account="acc2")

    # 200 on MSFT is fine — different ticker
    order = make_order(quantity=200, ticker="MSFT", account_id="acc1")
    checker.check(order)  # should not raise


def test_position_limit_independent_per_account():
    checker = RiskChecker(max_position=1000)
    # acc1 is at 900
    trade = make_trade(buyer_order_id="O-1", quantity=900, ticker="AAPL")
    checker.record_fill(trade, buyer_account="acc1", seller_account="acc2")

    # acc2's order for 1000 shares is fine — different account
    order = make_order(quantity=1000, account_id="acc2")
    checker.check(order)  # should not raise


# ── Notional exposure tests ───────────────────────────────────────────────────

def test_notional_exposure_allows_order_within_limit():
    checker = RiskChecker(max_notional=Decimal("100_000"))
    # 500 shares × $100 = $50,000 — within $100k limit
    order = make_order(quantity=500, price="100.00")
    checker.check(order)  # should not raise


def test_notional_exposure_rejects_order_exceeding_limit():
    checker = RiskChecker(max_notional=Decimal("100_000"))
    # 2000 shares × $100 = $200,000 — exceeds $100k
    order = make_order(quantity=2000, price="100.00")
    with pytest.raises(RiskViolation, match="Notional exposure limit exceeded"):
        checker.check(order)


def test_notional_exposure_accumulates_across_open_orders():
    checker = RiskChecker(max_notional=Decimal("100_000"))
    # First order: 600 × $100 = $60,000
    order1 = make_order(quantity=600, price="100.00", order_id="O-1")
    checker.check(order1)
    checker.record_open_order(order1)

    # Second order: 500 × $100 = $50,000; total = $110,000 > $100,000
    order2 = make_order(quantity=500, price="100.00", order_id="O-2")
    with pytest.raises(RiskViolation, match="Notional exposure limit exceeded"):
        checker.check(order2)


def test_notional_exposure_reduced_after_cancel():
    checker = RiskChecker(max_notional=Decimal("100_000"))
    # Open order: 600 × $100 = $60,000
    order1 = make_order(quantity=600, price="100.00", order_id="O-1")
    checker.check(order1)
    checker.record_open_order(order1)

    # Cancel it — exposure drops to $0
    checker.record_cancel(order1)

    # Now a $70,000 order is fine
    order2 = make_order(quantity=700, price="100.00", order_id="O-2")
    checker.check(order2)  # should not raise


def test_notional_exposure_market_orders_not_checked():
    checker = RiskChecker(max_notional=Decimal("100"))
    # Market order with massive quantity — notional is not checked
    order = make_order(quantity=999_999, price=None, order_type=OrderType.MARKET)
    checker.check(order)  # should not raise (position check will pass too for small account)


def test_get_position_returns_zero_for_unknown_account():
    checker = RiskChecker()
    assert checker.get_position("unknown_account", "AAPL") == 0


def test_get_notional_exposure_returns_zero_for_unknown_account():
    checker = RiskChecker()
    assert checker.get_notional_exposure("unknown_account") == Decimal("0")


# ── API integration tests ─────────────────────────────────────────────────────

async def test_api_rejects_order_exceeding_position_limit(client):
    """POST /orders returns 422 when position limit is breached."""
    # Submit MAX_POSITION_QUANTITY + 1 shares
    response = await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": MAX_POSITION_QUANTITY + 1,
        "price": "1.00",
    })
    assert response.status_code == 422
    assert "Position limit exceeded" in response.json()["detail"]


async def test_api_rejects_order_exceeding_notional_limit(client):
    """POST /orders returns 422 when notional exposure limit is breached."""
    # 10,000 shares × $200 = $2,000,000 > $1,000,000 limit
    response = await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10_000,
        "price": "200.00",
    })
    assert response.status_code == 422
    assert "Notional exposure limit exceeded" in response.json()["detail"]
```

The `client` fixture is already defined in `test_api.py` but each test file needs its
own fixture or a shared conftest. Add a `conftest.py`:

**File:** `trading/tests/conftest.py`

```python
"""
Shared test fixtures.
"""
import pytest
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.fixture
async def client():
    """
    Full-lifespan async test client.

    Starts the app lifespan (consumer + risk checker + broadcaster)
    for each test, giving a clean isolated state.
    """
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as ac:
            yield ac
```

Then remove the duplicate `client` fixture from `test_api.py` and `test_websocket.py`
(the managed_app fixture in test_websocket.py stays — it's different).

**Run:**
```bash
pytest trading/tests/test_risk.py -v
pytest -q  # all tests still pass
```

**Success criteria:** All risk unit tests pass; API tests still pass with conftest.py
providing the shared `client` fixture.

---

### Day 1 Checklist
- [ ] `trading/risk/checker.py` created with `RiskChecker` and `RiskViolation`
- [ ] `trading/api/consumer.py` updated with risk checking
- [ ] `trading/api/dependencies.py` updated with `_risk` singleton and `get_risk()`
- [ ] `main.py` updated to init risk and pass to consumer
- [ ] `trading/api/routes.py` updated to handle `RiskViolation` and record cancels
- [ ] `trading/engine/matcher.py` exposes `order_registry` publicly if needed
- [ ] `trading/tests/conftest.py` created with shared `client` fixture
- [ ] Duplicate `client` fixture removed from `test_api.py`
- [ ] All tests pass (`pytest -v`)

---

## Day 2: Market Order Price Protection (2 hours)

### Goal
Reject market orders when the spread is dangerously wide (or book is empty),
protecting clients from unexpected execution prices.

---

### Step 2.1: Add Spread Protection to RiskChecker (1 hour)

Extend `RiskChecker` with a spread-check method. This is a pure read — no state update
needed.

**File:** `trading/risk/checker.py` — add to `RiskChecker`:

```python
MAX_SPREAD: Decimal = Decimal("50.00")   # reject market orders if spread > $50


def check_market_spread(
    self,
    ticker: str,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
) -> None:
    """
    Reject a market order if the book is empty or spread exceeds MAX_SPREAD.

    Called from the consumer before executing a MARKET order.

    Raises:
        RiskViolation: if book has no liquidity or spread is too wide.
    """
    if best_bid is None and best_ask is None:
        raise RiskViolation(
            f"Market order rejected for {ticker}: order book is empty"
        )
    if best_bid is None or best_ask is None:
        # One-sided book — we can still execute against the available side.
        # Only reject if there is literally no liquidity on the relevant side.
        return
    spread = best_ask - best_bid
    if spread > self._max_spread:
        raise RiskViolation(
            f"Market order rejected for {ticker}: spread {spread} "
            f"exceeds limit {self._max_spread}"
        )
```

Also add `max_spread` to `__init__`:

```python
def __init__(
    self,
    max_position: int = MAX_POSITION_QUANTITY,
    max_notional: Decimal = MAX_NOTIONAL_EXPOSURE,
    max_spread: Decimal = MAX_SPREAD,
) -> None:
    ...
    self._max_spread = max_spread
```

**File:** `trading/api/consumer.py` — call `check_market_spread` for MARKET orders:

```python
from trading.events.models import OrderType

# In run_consumer, after risk.check(order) and before engine.submit_order():
if order.order_type == OrderType.MARKET:
    book = engine.manager.get_order_book(order.ticker)
    risk.check_market_spread(
        ticker=order.ticker,
        best_bid=book.get_best_bid(),
        best_ask=book.get_best_ask(),
    )
```

---

### Step 2.2: Write Spread Protection Tests (1 hour)

Add to `trading/tests/test_risk.py`:

```python
# ── Spread protection tests ───────────────────────────────────────────────────

def test_spread_check_passes_with_tight_spread():
    checker = RiskChecker(max_spread=Decimal("10.00"))
    checker.check_market_spread("AAPL", Decimal("99.00"), Decimal("100.00"))  # spread = 1.00


def test_spread_check_rejects_wide_spread():
    checker = RiskChecker(max_spread=Decimal("10.00"))
    with pytest.raises(RiskViolation, match="spread"):
        checker.check_market_spread("AAPL", Decimal("50.00"), Decimal("150.00"))  # spread = 100


def test_spread_check_rejects_empty_book():
    checker = RiskChecker()
    with pytest.raises(RiskViolation, match="empty"):
        checker.check_market_spread("AAPL", None, None)


def test_spread_check_allows_one_sided_book():
    checker = RiskChecker()
    # Ask exists but no bid — still allows (will REJECT at matching engine if no liquidity)
    checker.check_market_spread("AAPL", None, Decimal("150.00"))  # should not raise


async def test_api_market_order_rejected_on_empty_book(client):
    """Market order on an empty book returns 422 (spread check: empty book)."""
    response = await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": 10,
    })
    assert response.status_code == 422
    assert "empty" in response.json()["detail"].lower()


async def test_api_market_order_executes_on_tight_spread(client):
    """Market order succeeds when the book has a tight spread."""
    # Add a sell limit
    await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "SELL",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": "150.01",
    })
    # Add a buy limit
    await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": "149.99",
    })

    # Market order — spread = $0.02, well within $50 limit
    response = await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": 5,
    })
    assert response.status_code == 201
```

**Run:**
```bash
pytest trading/tests/test_risk.py -v
```

---

### Day 2 Checklist
- [ ] `check_market_spread()` added to `RiskChecker`
- [ ] Consumer calls spread check before MARKET order submission
- [ ] Spread protection tests added and passing
- [ ] All existing tests still pass (`pytest -q`)

---

## Day 3: Event Log (4 hours)

### Goal
Write every domain event (order submitted, trade executed, order cancelled) to an
append-only log file. Each line is a JSON object. The log is the source of truth for
recovery in Day 4.

---

### Step 3.1: Design the Event Schema (30 min)

Three event types, each as a JSON object on its own line (NDJSON format):

```json
{"event": "order_submitted", "ts": "2024-01-15T10:00:00Z", "order": {...full Order fields...}}
{"event": "trade_executed",  "ts": "2024-01-15T10:00:01Z", "trade": {...full Trade fields...}}
{"event": "order_cancelled", "ts": "2024-01-15T10:00:02Z", "order_id": "...", "ticker": "AAPL"}
```

The log is append-only: one line per event, never modified, never deleted during a run.
On recovery, the log is replayed sequentially to rebuild in-memory state.

---

### Step 3.2: Implement EventLog (2 hours)
**File:** `trading/persistence/event_log.py`

```python
"""
Append-only event log for the trading platform.

Every domain event is written as a JSON line (NDJSON) to a log file.
The log is the authoritative record for snapshot + replay recovery.

Event types:
  order_submitted — recorded before submit_order() (ensures all attempts are logged)
  trade_executed  — recorded once per trade
  order_cancelled — recorded after a successful cancellation
"""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator

import aiofiles

from trading.events.models import Order, Trade

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path("data/events.log")


class EventLog:
    """
    Async append-only event log backed by a flat file.

    One instance is shared for the lifetime of the application.
    All writes go through append(), which is called from the single
    consumer coroutine — no locking needed.
    """

    def __init__(self, path: Path = DEFAULT_LOG_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def append_order_submitted(self, order: Order) -> None:
        """Log an order submission attempt."""
        await self._write({
            "event": "order_submitted",
            "ts": _now(),
            "order": _order_to_dict(order),
        })

    async def append_trade_executed(self, trade: Trade) -> None:
        """Log a confirmed trade."""
        await self._write({
            "event": "trade_executed",
            "ts": _now(),
            "trade": _trade_to_dict(trade),
        })

    async def append_order_cancelled(self, order_id: str, ticker: str) -> None:
        """Log a successful cancellation."""
        await self._write({
            "event": "order_cancelled",
            "ts": _now(),
            "order_id": order_id,
            "ticker": ticker,
        })

    async def read_all(self) -> AsyncIterator[dict[str, Any]]:
        """
        Yield all events from the log in order.

        Used during recovery to replay events after loading a snapshot.
        """
        if not self._path.exists():
            return
        async with aiofiles.open(self._path, "r") as f:
            async for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    async def _write(self, event: dict[str, Any]) -> None:
        async with aiofiles.open(self._path, "a") as f:
            await f.write(json.dumps(event, default=_json_default) + "\n")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _json_default(obj: Any) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Not serializable: {type(obj)}")


def _order_to_dict(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "ticker": order.ticker,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "quantity": order.quantity,
        "price": str(order.price) if order.price is not None else None,
        "timestamp": order.timestamp.isoformat(),
        "status": order.status.value,
        "filled_quantity": order.filled_quantity,
        "account_id": order.account_id,
    }


def _trade_to_dict(trade: Trade) -> dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "ticker": trade.ticker,
        "buyer_order_id": trade.buyer_order_id,
        "seller_order_id": trade.seller_order_id,
        "price": str(trade.price),
        "quantity": trade.quantity,
        "timestamp": trade.timestamp.isoformat(),
    }
```

**Validation:**
```bash
python -c "from trading.persistence.event_log import EventLog; print('OK')"
```

---

### Step 3.3: Integrate EventLog Into the Consumer (30 min)

**File:** `trading/api/consumer.py` — add `event_log` parameter and write events:

```python
from trading.persistence.event_log import EventLog

async def run_consumer(
    engine: MatchingEngine,
    queue: asyncio.Queue,
    broadcaster: Broadcaster,
    risk: RiskChecker,
    event_log: EventLog,
) -> None:
    ...
    # After risk.check() passes and before submit_order():
    await event_log.append_order_submitted(order)

    trades = engine.submit_order(order)
    future.set_result(trades)

    for trade in trades:
        await event_log.append_trade_executed(trade)
        # ... existing risk.record_fill() and broadcaster.notify_trade() ...
```

**File:** `trading/api/dependencies.py` — add event log singleton:

```python
from trading.persistence.event_log import EventLog

_event_log: EventLog | None = None


def get_event_log() -> EventLog:
    if _event_log is None:
        raise RuntimeError("EventLog not initialized. Call init_app_state() first.")
    return _event_log


def init_app_state() -> tuple[MatchingEngine, asyncio.Queue, RiskChecker, EventLog]:
    global _engine, _order_queue, _risk, _event_log
    _engine = MatchingEngine(SUPPORTED_TICKERS)
    _order_queue = asyncio.Queue()
    _risk = RiskChecker()
    _event_log = EventLog()
    return _engine, _order_queue, _risk, _event_log
```

**File:** `main.py` — pass event_log to consumer:

```python
engine, queue, risk, event_log = init_app_state()
broadcaster = init_broadcaster()

consumer_task = asyncio.create_task(
    consumer.run_consumer(engine, queue, broadcaster, risk, event_log),
    name="order-consumer"
)
```

**File:** `trading/api/routes.py` — log cancellations:

```python
from trading.api.dependencies import get_event_log
from trading.persistence.event_log import EventLog

@router.post("/orders/{order_id}/cancel", ...)
async def cancel_order(
    order_id: str,
    engine: MatchingEngine = Depends(get_engine),
    risk: RiskChecker = Depends(get_risk),
    event_log: EventLog = Depends(get_event_log),
) -> CancelResponse:
    ticker_and_order = engine.order_registry.get(order_id)
    success = engine.cancel_order(order_id)
    if success and ticker_and_order is not None:
        _, order = ticker_and_order
        risk.record_cancel(order)
        await event_log.append_order_cancelled(order_id, order.ticker)
    return CancelResponse(...)
```

---

### Step 3.4: Write EventLog Tests (1 hour)
**File:** `trading/tests/test_persistence.py`

```python
"""
Tests for event log persistence.
"""

import json
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

from trading.persistence.event_log import EventLog
from trading.events.models import Order, OrderSide, OrderType, Trade


def make_order(order_id: str = "O-1") -> Order:
    return Order(
        order_id=order_id,
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now(tz=timezone.utc),
        account_id="acc1",
    )


def make_trade() -> Trade:
    return Trade(
        trade_id="T-1",
        ticker="AAPL",
        buyer_order_id="O-1",
        seller_order_id="O-2",
        price=Decimal("150.00"),
        quantity=100,
        timestamp=datetime.now(tz=timezone.utc),
    )


@pytest.fixture
def tmp_log(tmp_path: Path) -> EventLog:
    """EventLog backed by a temporary directory (isolated per test)."""
    return EventLog(path=tmp_path / "events.log")


async def test_append_order_submitted(tmp_log, tmp_path):
    order = make_order()
    await tmp_log.append_order_submitted(order)

    log_file = tmp_path / "events.log"
    assert log_file.exists()
    event = json.loads(log_file.read_text().strip())
    assert event["event"] == "order_submitted"
    assert event["order"]["order_id"] == order.order_id
    assert event["order"]["ticker"] == "AAPL"
    assert event["order"]["price"] == "150.00"


async def test_append_trade_executed(tmp_log, tmp_path):
    trade = make_trade()
    await tmp_log.append_trade_executed(trade)

    event = json.loads((tmp_path / "events.log").read_text().strip())
    assert event["event"] == "trade_executed"
    assert event["trade"]["trade_id"] == trade.trade_id
    assert event["trade"]["price"] == "150.00"
    assert event["trade"]["quantity"] == 100


async def test_append_order_cancelled(tmp_log, tmp_path):
    await tmp_log.append_order_cancelled("O-1", "AAPL")

    event = json.loads((tmp_path / "events.log").read_text().strip())
    assert event["event"] == "order_cancelled"
    assert event["order_id"] == "O-1"
    assert event["ticker"] == "AAPL"


async def test_events_are_appended_in_order(tmp_log, tmp_path):
    order = make_order()
    trade = make_trade()

    await tmp_log.append_order_submitted(order)
    await tmp_log.append_trade_executed(trade)
    await tmp_log.append_order_cancelled("O-2", "MSFT")

    lines = (tmp_path / "events.log").read_text().strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event"] == "order_submitted"
    assert json.loads(lines[1])["event"] == "trade_executed"
    assert json.loads(lines[2])["event"] == "order_cancelled"


async def test_read_all_yields_events_in_order(tmp_log):
    order = make_order()
    trade = make_trade()

    await tmp_log.append_order_submitted(order)
    await tmp_log.append_trade_executed(trade)

    events = [e async for e in tmp_log.read_all()]
    assert len(events) == 2
    assert events[0]["event"] == "order_submitted"
    assert events[1]["event"] == "trade_executed"


async def test_read_all_returns_empty_for_missing_file(tmp_path):
    log = EventLog(path=tmp_path / "nonexistent.log")
    events = [e async for e in log.read_all()]
    assert events == []


async def test_market_order_price_is_null(tmp_log, tmp_path):
    order = Order(
        order_id="O-market",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=50,
        price=None,
        timestamp=datetime.now(tz=timezone.utc),
        account_id="acc1",
    )
    await tmp_log.append_order_submitted(order)

    event = json.loads((tmp_path / "events.log").read_text().strip())
    assert event["order"]["price"] is None
```

**Run:**
```bash
pytest trading/tests/test_persistence.py -v
```

---

### Day 3 Checklist
- [ ] `trading/persistence/event_log.py` created with `EventLog`
- [ ] Consumer updated to write `order_submitted` and `trade_executed` events
- [ ] Routes updated to write `order_cancelled` events
- [ ] `dependencies.py` updated with `_event_log` singleton
- [ ] `main.py` updated to init and pass event log
- [ ] `data/` directory created automatically on first write
- [ ] All event log tests pass (`pytest trading/tests/test_persistence.py -v`)
- [ ] All 104+ tests still pass

---

## Day 4: Snapshot + Replay (4 hours)

### Goal
On startup, load the latest snapshot (if any), then replay all events logged after
the snapshot was taken. The result is identical in-memory state to what existed before
shutdown.

---

### Step 4.1: Design Snapshot Format (30 min)

A snapshot captures the full order book state at a point in time as a JSON file:

```json
{
  "version": 1,
  "ts": "2024-01-15T10:00:00Z",
  "sequence": 1042,
  "books": {
    "AAPL": {
      "bids": [
        {"order_id": "...", "price": "149.50", "quantity": 100, "filled": 0, "account_id": "...", "timestamp": "...", "side": "BUY", "order_type": "LIMIT"}
      ],
      "asks": [...]
    },
    ...
  }
}
```

`sequence` is a monotonically increasing event counter written to every log line and
snapshot. On replay, only events with `sequence > snapshot["sequence"]` are re-applied.

This requires adding a `sequence` field to the `EventLog`.

---

### Step 4.2: Add Sequence Numbers to EventLog (30 min)
**File:** `trading/persistence/event_log.py` — add sequence counter:

```python
class EventLog:
    def __init__(self, path: Path = DEFAULT_LOG_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence: int = 0   # incremented before each write

    async def append_order_submitted(self, order: Order) -> int:
        """Returns the sequence number assigned to this event."""
        seq = self._next_seq()
        await self._write({
            "event": "order_submitted",
            "seq": seq,
            "ts": _now(),
            "order": _order_to_dict(order),
        })
        return seq

    async def append_trade_executed(self, trade: Trade) -> int:
        seq = self._next_seq()
        await self._write({
            "event": "trade_executed",
            "seq": seq,
            "ts": _now(),
            "trade": _trade_to_dict(trade),
        })
        return seq

    async def append_order_cancelled(self, order_id: str, ticker: str) -> int:
        seq = self._next_seq()
        await self._write({
            "event": "order_cancelled",
            "seq": seq,
            "ts": _now(),
            "order_id": order_id,
            "ticker": ticker,
        })
        return seq

    def _next_seq(self) -> int:
        self._sequence += 1
        return self._sequence
```

Also add `after_sequence` parameter to `read_all()` for selective replay:

```python
async def read_all(self, after_sequence: int = 0) -> AsyncIterator[dict[str, Any]]:
    """Yield events with seq > after_sequence."""
    if not self._path.exists():
        return
    async with aiofiles.open(self._path, "r") as f:
        async for line in f:
            line = line.strip()
            if line:
                event = json.loads(line)
                if event.get("seq", 0) > after_sequence:
                    yield event
```

---

### Step 4.3: Implement Snapshot (2 hours)
**File:** `trading/persistence/snapshot.py`

```python
"""
Snapshot writer and loader for the order book state.

A snapshot is a JSON file capturing all resting orders in every book,
along with the event log sequence number at the time of the snapshot.
On startup, load the snapshot, then replay only events after its sequence.
"""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiofiles

from trading.engine.matcher import MatchingEngine
from trading.events.models import Order, OrderSide, OrderType, OrderStatus

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_PATH = Path("data/snapshot.json")
SNAPSHOT_VERSION = 1


class SnapshotManager:
    """Saves and restores full order book state."""

    def __init__(self, path: Path = DEFAULT_SNAPSHOT_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def save(self, engine: MatchingEngine, sequence: int) -> None:
        """
        Write a snapshot of all resting orders to disk.

        Called periodically (e.g. every N events or on graceful shutdown).
        Overwrites the previous snapshot — only the latest is needed.
        """
        snapshot = {
            "version": SNAPSHOT_VERSION,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "sequence": sequence,
            "books": {},
        }

        for ticker in engine.get_supported_tickers():
            book = engine.manager.get_order_book(ticker)
            snapshot["books"][ticker] = {
                "bids": [_order_to_dict(o) for orders in book.bids.values() for o in orders],
                "asks": [_order_to_dict(o) for orders in book.asks.values() for o in orders],
            }

        async with aiofiles.open(self._path, "w") as f:
            await f.write(json.dumps(snapshot, indent=2))

        logger.info("Snapshot saved at sequence %d to %s", sequence, self._path)

    async def load(self) -> dict[str, Any] | None:
        """
        Load the most recent snapshot from disk.

        Returns None if no snapshot exists.
        """
        if not self._path.exists():
            return None
        async with aiofiles.open(self._path, "r") as f:
            content = await f.read()
        snapshot = json.loads(content)
        if snapshot.get("version") != SNAPSHOT_VERSION:
            logger.warning("Snapshot version mismatch, ignoring")
            return None
        return snapshot

    def restore(self, engine: MatchingEngine, snapshot: dict[str, Any]) -> None:
        """
        Restore all resting orders from a snapshot into the engine.

        Called synchronously during startup before the consumer starts.
        """
        for ticker, book_data in snapshot.get("books", {}).items():
            for order_dict in book_data.get("bids", []) + book_data.get("asks", []):
                order = _dict_to_order(order_dict)
                # Directly add to the order book (bypass matching — snapshot orders
                # are resting and would not match each other at snapshot time)
                book = engine.manager.get_order_book(ticker)
                book._add_to_book(order)
                engine.order_registry[order.order_id] = (ticker, order)

        logger.info(
            "Snapshot restored: sequence=%d, tickers=%s",
            snapshot.get("sequence", 0),
            list(snapshot.get("books", {}).keys()),
        )
```

**Note on `book._add_to_book`**: The OrderBook may not have this method yet. You will
need to extract the "add to resting side" logic into a helper in Day 4 Step 4.4.

---

### Step 4.4: Add _add_to_book Helper to OrderBook (30 min)
**File:** `trading/engine/order_book.py`

Currently `add_limit_order()` both matches AND rests the order. For snapshot restore
we only want to add a resting order without triggering matching. Add a private helper:

```python
def _add_to_book(self, order: Order) -> None:
    """
    Place a resting order directly into the book without matching.

    Used only during snapshot restore. The order must already have
    status=NEW and remaining quantity > 0.
    """
    if order.side == OrderSide.BUY:
        if order.price not in self.bids:
            self.bids[order.price] = deque()
        self.bids[order.price].append(order)
    else:
        if order.price not in self.asks:
            self.asks[order.price] = deque()
        self.asks[order.price].append(order)
```

Also expose `order_registry` on `MatchingEngine` as a public dict (not via property)
so snapshot restore can write to it directly. Update `matcher.py`:

```python
# Change from:
self._order_registry: dict[str, tuple[str, Order]] = {}
# To:
self.order_registry: dict[str, tuple[str, Order]] = {}
# And update all internal references from _order_registry to order_registry.
```

---

### Step 4.5: Wire Snapshot Into main.py (30 min)

**File:** `trading/api/dependencies.py` — add snapshot singleton:

```python
from trading.persistence.snapshot import SnapshotManager

_snapshot_manager: SnapshotManager | None = None

def get_snapshot_manager() -> SnapshotManager:
    if _snapshot_manager is None:
        raise RuntimeError("SnapshotManager not initialized.")
    return _snapshot_manager

def init_app_state() -> tuple[MatchingEngine, asyncio.Queue, RiskChecker, EventLog, SnapshotManager]:
    global _engine, _order_queue, _risk, _event_log, _snapshot_manager
    _engine = MatchingEngine(SUPPORTED_TICKERS)
    _order_queue = asyncio.Queue()
    _risk = RiskChecker()
    _event_log = EventLog()
    _snapshot_manager = SnapshotManager()
    return _engine, _order_queue, _risk, _event_log, _snapshot_manager
```

**File:** `main.py` — add startup restore and periodic snapshot:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    engine, queue, risk, event_log, snapshot_mgr = init_app_state()
    broadcaster = init_broadcaster()

    # ── Startup: restore from snapshot + replay event log ──────────────────
    snapshot = await snapshot_mgr.load()
    replay_after = 0
    if snapshot:
        snapshot_mgr.restore(engine, snapshot)
        replay_after = snapshot["sequence"]
        logger.info("Restored from snapshot at sequence %d", replay_after)

    async for event in event_log.read_all(after_sequence=replay_after):
        await _replay_event(engine, event)
    logger.info("Event log replay complete")

    # ── Background tasks ──────────────────────────────────────────────────
    consumer_task = asyncio.create_task(
        consumer.run_consumer(engine, queue, broadcaster, risk, event_log),
        name="order-consumer"
    )
    snapshot_task = asyncio.create_task(
        _periodic_snapshot(engine, event_log, snapshot_mgr),
        name="snapshot"
    )

    yield

    # ── Shutdown: final snapshot ───────────────────────────────────────────
    snapshot_task.cancel()
    consumer_task.cancel()
    for task in [snapshot_task, consumer_task]:
        try:
            await task
        except asyncio.CancelledError:
            pass
    await snapshot_mgr.save(engine, event_log._sequence)
    logger.info("Final snapshot saved on shutdown")


async def _replay_event(engine: MatchingEngine, event: dict) -> None:
    """Re-apply a single logged event to rebuild in-memory state."""
    from trading.events.models import OrderSide, OrderType, OrderStatus
    from decimal import Decimal

    if event["event"] == "order_submitted":
        od = event["order"]
        order = Order(
            order_id=od["order_id"],
            ticker=od["ticker"],
            side=OrderSide(od["side"]),
            order_type=OrderType(od["order_type"]),
            quantity=od["quantity"],
            price=Decimal(od["price"]) if od["price"] else None,
            timestamp=datetime.fromisoformat(od["timestamp"]),
            status=OrderStatus(od["status"]),
            filled_quantity=od["filled_quantity"],
            account_id=od["account_id"],
        )
        engine.submit_order(order)

    elif event["event"] == "order_cancelled":
        engine.cancel_order(event["order_id"])


async def _periodic_snapshot(
    engine: MatchingEngine,
    event_log: EventLog,
    snapshot_mgr: SnapshotManager,
    interval_seconds: int = 300,
) -> None:
    """Write a snapshot every `interval_seconds` (default 5 minutes)."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await snapshot_mgr.save(engine, event_log._sequence)
        except asyncio.CancelledError:
            break
```

Note: `_replay_event` does **not** re-check risk or re-write to the log — it only
rebuilds engine state. Trade events are not replayed directly because `engine.submit_order()`
during replay will generate the same trades naturally.

**Validation:**
```bash
python -c "from main import app; print('OK')"
pytest -q
```

---

### Step 4.6: Write Snapshot Tests (1 hour)

Add to `trading/tests/test_persistence.py`:

```python
from trading.persistence.snapshot import SnapshotManager
from trading.engine.matcher import MatchingEngine
from trading.api.dependencies import SUPPORTED_TICKERS


def make_engine() -> MatchingEngine:
    return MatchingEngine(SUPPORTED_TICKERS)


async def test_save_and_load_empty_engine(tmp_path):
    engine = make_engine()
    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(engine, sequence=0)

    snapshot = await mgr.load()
    assert snapshot is not None
    assert snapshot["sequence"] == 0
    assert set(snapshot["books"].keys()) == set(SUPPORTED_TICKERS)


async def test_snapshot_captures_resting_orders(tmp_path):
    from trading.events.models import Order, OrderSide, OrderType
    from datetime import datetime, timezone

    engine = make_engine()
    order = Order(
        order_id="O-snap",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now(tz=timezone.utc),
        account_id="acc1",
    )
    engine.submit_order(order)

    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(engine, sequence=1)

    snapshot = await mgr.load()
    aapl_bids = snapshot["books"]["AAPL"]["bids"]
    assert len(aapl_bids) == 1
    assert aapl_bids[0]["order_id"] == "O-snap"


async def test_restore_rebuilds_book(tmp_path):
    from trading.events.models import Order, OrderSide, OrderType
    from datetime import datetime, timezone

    engine1 = make_engine()
    order = Order(
        order_id="O-restore",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("149.00"),
        timestamp=datetime.now(tz=timezone.utc),
        account_id="acc1",
    )
    engine1.submit_order(order)

    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(engine1, sequence=1)

    # Restore into a fresh engine
    engine2 = make_engine()
    snapshot = await mgr.load()
    mgr.restore(engine2, snapshot)

    book = engine2.manager.get_order_book("AAPL")
    assert book.get_best_bid() == Decimal("149.00")
    assert "O-restore" in engine2.order_registry


async def test_load_returns_none_for_missing_snapshot(tmp_path):
    mgr = SnapshotManager(path=tmp_path / "no_snapshot.json")
    assert await mgr.load() is None


async def test_snapshot_not_returned_for_wrong_version(tmp_path):
    import json
    snap_path = tmp_path / "snapshot.json"
    snap_path.write_text(json.dumps({"version": 99, "sequence": 0, "books": {}}))

    mgr = SnapshotManager(path=snap_path)
    assert await mgr.load() is None
```

**Run:**
```bash
pytest trading/tests/test_persistence.py -v
pytest -q
```

---

### Day 4 Checklist
- [ ] `EventLog` updated with sequence numbers and `after_sequence` filter
- [ ] `trading/persistence/snapshot.py` created with `SnapshotManager`
- [ ] `OrderBook._add_to_book()` helper added
- [ ] `MatchingEngine.order_registry` made public (no underscore)
- [ ] `main.py` updated with snapshot restore on startup and periodic save
- [ ] `_replay_event()` helper in `main.py`
- [ ] All persistence tests pass
- [ ] All 104+ tests still pass (`pytest -q`)

---

## Day 5: Idempotency (3 hours)

### Goal
Reject duplicate `order_id` submissions. A client that retries a POST should receive
the same response rather than creating a duplicate order.

---

### Step 5.1: Design the Idempotency Store (30 min)

**What to store:**
```python
# order_id → {"trades": [...], "order": {...}} (the original result)
_seen: dict[str, dict]
```

**Where to check:** In `routes.py`, before enqueuing — a duplicate is rejected at the
HTTP layer immediately, never reaching the consumer.

**Eviction:** For simplicity, store in-memory without eviction. A production system
would use a TTL (e.g. 24 hours) and a persistent store (Redis or the event log itself).
Document this as a known limitation.

**Note:** The idempotency store is checked and written in the HTTP handler (not the
consumer) because we want to return the cached response synchronously without touching
the queue.

---

### Step 5.2: Implement IdempotencyStore (45 min)
**File:** `trading/api/dependencies.py` — add to existing module:

```python
class IdempotencyStore:
    """
    In-memory store for idempotent order submission.

    Maps order_id → the OrderResponse dict returned on first submission.
    Duplicate submissions with the same order_id return the cached response
    with HTTP 200 (not 201) to signal it's a replay, not a new order.

    Limitation: unbounded in-memory growth. Production would use a TTL
    and persistent backing store.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    def get(self, order_id: str) -> dict | None:
        return self._cache.get(order_id)

    def store(self, order_id: str, response: dict) -> None:
        self._cache[order_id] = response

    def __contains__(self, order_id: str) -> bool:
        return order_id in self._cache


_idempotency_store: IdempotencyStore | None = None


def get_idempotency_store() -> IdempotencyStore:
    if _idempotency_store is None:
        raise RuntimeError("IdempotencyStore not initialized.")
    return _idempotency_store


# Update init_app_state to also init the store:
def init_app_state() -> tuple[...]:
    global ..., _idempotency_store
    ...
    _idempotency_store = IdempotencyStore()
    return ..., _idempotency_store
```

---

### Step 5.3: Wire Idempotency Into submit_order Route (30 min)
**File:** `trading/api/routes.py`

```python
from trading.api.dependencies import get_idempotency_store, IdempotencyStore
from fastapi.responses import JSONResponse

@router.post("/orders", ...)
async def submit_order(
    request: OrderRequest,
    engine: MatchingEngine = Depends(get_engine),
    queue: asyncio.Queue = Depends(get_order_queue),
    idempotency: IdempotencyStore = Depends(get_idempotency_store),
) -> OrderResponse:
    # Check idempotency before any processing
    if request.order_id is not None and request.order_id in idempotency:
        cached = idempotency.get(request.order_id)
        return JSONResponse(content=cached, status_code=status.HTTP_200_OK)

    # ... existing submission logic ...

    response = OrderResponse(...)
    if request.order_id is not None:
        idempotency.store(request.order_id, response.model_dump(mode="json"))
    return response
```

**Update `OrderRequest` schema** to accept an optional `order_id`:
**File:** `trading/api/schemas.py`:

```python
class OrderRequest(BaseModel):
    order_id: Optional[str] = Field(
        None,
        description="Client-supplied order ID for idempotent submission. "
                    "If omitted, a UUID is generated server-side."
    )
    ticker: str = ...
    # ... rest unchanged
```

**Update `routes.py`** to use the client-supplied `order_id` if provided:

```python
order = Order(
    order_id=request.order_id or str(uuid.uuid4()),
    ...
)
```

---

### Step 5.4: Write Idempotency Tests (1 hour)
**File:** `trading/tests/test_idempotency.py`

```python
"""
Tests for idempotent order submission.
"""

import pytest
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
from main import app


async def test_duplicate_order_id_returns_same_response(client):
    """Submitting the same order_id twice returns the first result."""
    payload = {
        "order_id": "my-idempotency-key",
        "ticker": "AAPL",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": "150.00",
    }

    resp1 = await client.post("/orders", json=payload)
    assert resp1.status_code == 201

    resp2 = await client.post("/orders", json=payload)
    assert resp2.status_code == 200  # replay, not new order
    assert resp2.json()["order_id"] == resp1.json()["order_id"]
    assert resp2.json()["status"] == resp1.json()["status"]


async def test_duplicate_does_not_create_second_order(client):
    """The book should only have one resting order after two identical submissions."""
    payload = {
        "order_id": "idem-key-2",
        "ticker": "MSFT",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 20,
        "price": "299.00",
    }

    await client.post("/orders", json=payload)
    await client.post("/orders", json=payload)  # duplicate

    book = await client.get("/book/MSFT")
    bids = book.json()["bids"]
    bid_at_299 = next((b for b in bids if b["price"] == "299.00"), None)
    assert bid_at_299 is not None
    assert bid_at_299["quantity"] == 20  # only 20, not 40


async def test_different_order_ids_create_separate_orders(client):
    """Two different order_ids with the same payload create two separate orders."""
    base = {
        "ticker": "GOOGL",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 5,
        "price": "2500.00",
    }

    resp1 = await client.post("/orders", json={**base, "order_id": "key-A"})
    resp2 = await client.post("/orders", json={**base, "order_id": "key-B"})

    assert resp1.status_code == 201
    assert resp2.status_code == 201
    assert resp1.json()["order_id"] != resp2.json()["order_id"]


async def test_order_without_order_id_is_not_cached(client):
    """Orders without a client-supplied order_id are never cached (no dedup)."""
    payload = {
        "ticker": "TSLA",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 5,
        "price": "200.00",
    }

    resp1 = await client.post("/orders", json=payload)
    resp2 = await client.post("/orders", json=payload)

    assert resp1.status_code == 201
    assert resp2.status_code == 201
    # Two separate orders with different server-generated IDs
    assert resp1.json()["order_id"] != resp2.json()["order_id"]
```

**Run:**
```bash
pytest trading/tests/test_idempotency.py -v
pytest -q  # full suite
```

---

### Day 5 Checklist
- [ ] `IdempotencyStore` added to `trading/api/dependencies.py`
- [ ] `OrderRequest.order_id` optional field added to `schemas.py`
- [ ] `submit_order` route checks idempotency store and caches responses
- [ ] `routes.py` uses client-supplied `order_id` if provided
- [ ] `init_app_state()` initialises `IdempotencyStore`
- [ ] All idempotency tests pass
- [ ] All tests pass (`pytest -q`)

---

## Day 6: Final Validation & Documentation (2 hours)

### Step 6.1: Run Full Test Suite

```bash
pytest -v --tb=short
pytest --collect-only -q  # verify test count
```

Expected: 104 existing + ~30 new = ~134 tests.

### Step 6.2: Code Quality

```bash
black trading/ main.py
ruff check trading/ main.py --fix
mypy trading/ --ignore-missing-imports
```

Fix any issues before committing.

### Step 6.3: Manual Smoke Test

```bash
# Start server
uvicorn main:app --port 8000

# Confirm snapshot created on startup (data/ should exist)
ls data/

# Submit orders
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"order_id":"test-idem","ticker":"AAPL","side":"BUY","order_type":"LIMIT","quantity":100,"price":"150.00"}' \
  | python -m json.tool

# Duplicate — should return 200 with same order_id
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"order_id":"test-idem","ticker":"AAPL","side":"BUY","order_type":"LIMIT","quantity":100,"price":"150.00"}' \
  | python -m json.tool

# Check event log
cat data/events.log | python -m json.tool

# Stop server (Ctrl+C) — snapshot written on shutdown
cat data/snapshot.json | python -m json.tool

# Restart — should restore from snapshot
uvicorn main:app --port 8000
# Book should still show the resting order
curl -s http://localhost:8000/book/AAPL | python -m json.tool
```

### Step 6.4: Write week3_summary.md

Use `week2_summary.md` as a template. Include:
1. What Was Built — risk checker, event log, snapshot, idempotency
2. Architecture diagram (updated to show risk + persistence path)
3. Design decisions — where risk checks live (consumer), why event log is NDJSON,
   snapshot sequence numbering, idempotency at HTTP layer
4. Test coverage table (new files + totals)
5. Known Limitations — no TTL on idempotency store, no log compaction, in-memory
   risk state lost on restart (separate from engine state), no auth
6. Validation checklist
7. Resume bullet

### Step 6.5: Commit All Work

```bash
git add trading/risk/ trading/persistence/ trading/tests/test_risk.py
git add trading/tests/test_persistence.py trading/tests/test_idempotency.py
git add trading/tests/conftest.py trading/api/ trading/engine/ main.py
git add week3_summary.md week3_execution.md

git commit -m "Add pre-trade risk checks (position limit, notional exposure, spread protection)"
git commit -m "Add append-only event log with sequence numbers"
git commit -m "Add snapshot/replay for deterministic restart recovery"
git commit -m "Add idempotent order submission"
git commit -m "Add week3 tests and documentation"
```

---

## Week 3 Final Validation

### Must Pass:
```bash
pytest -v                          # all tests green
python -c "from main import app; print('OK')"
uvicorn main:app --port 8000       # starts cleanly, restores from snapshot
```

### Success Criteria:
✅ Orders exceeding `MAX_POSITION_QUANTITY` (10,000 shares) return 422
✅ Orders exceeding `MAX_NOTIONAL_EXPOSURE` ($1M) return 422
✅ Market orders on empty book return 422 (spread protection)
✅ Every order, trade, and cancellation appears in `data/events.log`
✅ Restart restores identical order book state from snapshot + replay
✅ Duplicate `order_id` submissions return 200 with cached response
✅ All existing 104 tests still pass
✅ ~134 total tests passing

---

## Troubleshooting

### `aiofiles` not found
```bash
uv add aiofiles && uv add --dev types-aiofiles
```

### Snapshot restore fails with AttributeError
- Check that `OrderBook._add_to_book` is defined
- Check that `MatchingEngine.order_registry` is public (no underscore)

### Risk state incorrect after restart
Risk position state (`_positions`, `_open_orders`) is rebuilt from the event log
during replay — it is **not** stored in the snapshot. Verify that the event log replay
in `_replay_event` calls `risk.record_fill()` for each replayed trade.
If risk state is not replayed, add a `risk` parameter to `_replay_event`.

### Tests fail with "data/events.log" written during tests
Tests use `tmp_path` fixtures for `EventLog` and `SnapshotManager` — they should never
write to `data/`. If they do, the `EventLog` or `SnapshotManager` in `init_app_state()`
is using the default path. Confirm the `client` fixture triggers a full lifespan where
`init_app_state()` passes a temp path, or configure the log path via an environment
variable.

**Recommended fix**: Accept the log path via an environment variable with a default:

```python
import os
DEFAULT_LOG_PATH = Path(os.getenv("EVENT_LOG_PATH", "data/events.log"))
DEFAULT_SNAPSHOT_PATH = Path(os.getenv("SNAPSHOT_PATH", "data/snapshot.json"))
```

Then in tests (conftest.py):
```python
import os
os.environ["EVENT_LOG_PATH"] = str(tmp_path / "events.log")
os.environ["SNAPSHOT_PATH"] = str(tmp_path / "snapshot.json")
```

Or use `monkeypatch.setenv` per test.

---

## Week 4 Preview

- **Observability**: structured logging, Prometheus metrics (orders/sec, latency histograms,
  queue depth), `/health` and `/metrics` endpoints
- **Authentication**: API key middleware, per-key rate limiting
- **Log compaction**: rotate and archive old event log segments
- **Per-ticker consumer sharding**: one consumer coroutine per ticker to eliminate
  head-of-line blocking between unrelated symbols
- **WebSocket session resumption**: sequence-numbered events, client reconnect with
  `?since=N` to receive missed messages
