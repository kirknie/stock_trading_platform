# Week 4 Execution Plan: Resilience & Scalability

## Overview

| Day | Theme | Goal |
|-----|-------|------|
| 1 | Risk state recovery | Rebuild RiskChecker from event log on restart |
| 2 | Order registry eviction | Cap unbounded memory growth |
| 3 | Persistent idempotency | Survive restarts with idempotency cache |
| 4 | Per-ticker consumers | Shard matching queue by ticker |
| 5 | Tests & validation | Full test suite, mypy, smoke test |
| 6 | Documentation & commit | week4_summary.md, commit |

---

## Pre-week Setup

```bash
# Verify starting state
python -m pytest trading/tests/ -q
# Expected: 171 passed

mypy trading/ --ignore-missing-imports
# Expected: Success, no errors

# Confirm current known gaps
grep -n "Risk state" main.py
grep -n "unbounded" week3_summary.md
```

No new directories needed — all changes land in existing modules.

---

## Day 1: Risk State Recovery

### Background

After a restart, `RiskChecker._positions` and `RiskChecker._open_orders` are empty.
This means:
- Position limits are not enforced for pre-existing fills
- Notional exposure limits are not enforced for open orders

The event log already contains everything needed:
- `trade_executed` → rebuild `_positions`
- `order_submitted` → rebuild `_open_orders`
- `order_cancelled` + `order_completed` (derived) → remove from `_open_orders`

### Step 1.1 — Design (no code)

**Two-pass replay problem:**

`trade_executed` events only contain `buyer_order_id` and `seller_order_id`, not
`account_id`. To call `risk.record_fill(trade, buyer_account, seller_account)` we
need to look up the accounts in `engine.order_registry`.

But `order_registry` is only populated from `order_submitted` replay. So:

- **Pass 1** (already done in `_replay_event`): replay `order_submitted` and
  `order_cancelled` events — this rebuilds the engine book AND `order_registry`.
- **Pass 2** (new): a second pass over the same events calling risk methods:
  - `order_submitted` → `risk.record_open_order(order)` if order not yet complete
  - `trade_executed` → lookup accounts in `order_registry`, call `risk.record_fill()`
  - `order_cancelled` → call `risk.record_cancel(order)`

**Which orders go into `_open_orders` after replay?**

Only orders that are currently resting in the book — i.e., status `NEW` or
`PARTIALLY_FILLED`. After Pass 1 replay, `engine.order_registry` contains exactly
this set. So Pass 2 can iterate `order_registry` instead of re-scanning the log.

Simpler approach:
- Pass 1: same as now — `_replay_event` rebuilds the engine
- Pass 2: after Pass 1, iterate `engine.order_registry` to rebuild `_open_orders`,
  then do a single pass over `trade_executed` events (using `read_all`) to rebuild
  `_positions`

This avoids loading all events twice.

### Step 1.2 — Add `_rebuild_risk_from_registry` to `main.py`

After Pass 1 (`_replay_event` loop), add a function that:

1. Rebuilds `_open_orders` from `order_registry` — every resting order is open
2. Rebuilds `_positions` by replaying `trade_executed` events from the event log

Add to `main.py` directly above `_replay_event`:

```python
async def _rebuild_risk(
    risk: RiskChecker,
    engine: MatchingEngine,
    event_log: EventLog,
    after_sequence: int,
) -> None:
    """
    Rebuild RiskChecker state after engine replay.

    Called once at startup, after _replay_event has reconstructed the order
    book and order_registry.

    Two phases:
      1. Open notional exposure — iterate order_registry (all resting orders)
      2. Filled positions       — replay trade_executed events from the log
    """
    # Phase 1: rebuild _open_orders from what is currently resting in the book
    for order_id, (ticker, order) in engine.order_registry.items():
        if order.order_type == OrderType.LIMIT:
            risk.record_open_order(order)

    # Phase 2: rebuild _positions from trade_executed events
    async for event in event_log.read_all(after_sequence=0):
        if event["event"] != "trade_executed":
            continue
        t = event["trade"]
        buyer_entry = engine.order_registry.get(t["buyer_order_id"])
        seller_entry = engine.order_registry.get(t["seller_order_id"])
        # Accounts may not be in registry if orders were fully filled and evicted;
        # for now registry is not evicted so they should always be present.
        # If missing, skip — positions may be understated (acceptable limitation
        # until order registry eviction is implemented with account persistence).
        if buyer_entry is None and seller_entry is None:
            continue
        buyer_account = buyer_entry[1].account_id if buyer_entry else "unknown"
        seller_account = seller_entry[1].account_id if seller_entry else "unknown"
        from decimal import Decimal
        from trading.events.models import Trade
        from datetime import datetime
        trade = Trade(
            trade_id=t["trade_id"],
            ticker=t["ticker"],
            buyer_order_id=t["buyer_order_id"],
            seller_order_id=t["seller_order_id"],
            price=Decimal(t["price"]),
            quantity=t["quantity"],
            timestamp=datetime.fromisoformat(t["timestamp"]),
        )
        risk.record_fill(trade, buyer_account, seller_account)
```

**Wire into lifespan** — call `_rebuild_risk` after the `_replay_event` loop:

```python
# After: async for event in event_log.read_all(...): _replay_event(...)
await _rebuild_risk(risk, engine, event_log, replay_after)
logger.info("Risk state rebuilt")
```

**Note:** The imports (Trade, Decimal, datetime) should be moved to the top of `main.py`,
not inside the function. They are already imported in other modules; add them to
`main.py`'s top-level imports.

### Step 1.3 — Write `trading/tests/test_recovery.py`

Create a new test file covering the recovery scenarios. Use the `client` fixture
(which runs full lifespan). The pattern is:

1. Submit orders via HTTP (they are logged)
2. Trigger a restart by re-running the full lifespan — simulate with a second
   `LifespanManager` call on the same `tmp_path` env vars

For test isolation, write a second fixture `restarted_client` that creates a second
`LifespanManager` instance using the same `tmp_path`:

```python
@pytest.fixture
async def restarted_client(tmp_path, monkeypatch):
    """Simulate a restart: second lifespan, same event log and snapshot path."""
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as ac:
            yield ac
```

Tests to write:

```
test_position_limit_enforced_after_restart
  - Submit buy orders up to 90% of limit (9000 shares)
  - Restart
  - Submit order that would exceed limit if positions not rebuilt → expect 422

test_notional_exposure_enforced_after_restart
  - Submit LIMIT buy at $100 for 9000 shares ($900k open exposure)
  - Restart
  - Submit second LIMIT buy for $200k → expect 422 (would total $1.1M)

test_open_order_freed_after_cancel_and_restart
  - Submit LIMIT buy at $100 for 9000 shares
  - Cancel it
  - Restart
  - Submit LIMIT buy for $900k → expect 201 (exposure is $0 after cancel)

test_filled_order_position_rebuilt_after_restart
  - Submit SELL LIMIT 10 @ $150
  - Submit BUY LIMIT 10 @ $150 (matches, position = 10 for buyer)
  - Restart
  - Verify: GET /book/AAPL shows empty book (still correct)
  - Submit another BUY to push past position limit → verify limit is enforced
```

**Note on test complexity:** The `restarted_client` fixture works because both
`client` and `restarted_client` use the same `tmp_path` and the same `monkeypatch`
env vars — the second lifespan reads the log written by the first.

However, since `app` is a module-level singleton and `init_app_state()` uses module-level
globals, two `LifespanManager` instances within the same test function will share the
same module globals. Use **separate sequential `async with LifespanManager`** blocks
(not nested), ensuring the first lifespan fully shuts down before the second starts.

Restructure the fixture to allow this:

```python
async def test_position_limit_enforced_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))

    # First lifespan: submit order
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.post("/orders", json={...})
            assert resp.status_code == 201

    # Second lifespan: verify risk state rebuilt
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.post("/orders", json={...})  # should fail
            assert resp.status_code == 422
```

Run:
```bash
python -m pytest trading/tests/test_recovery.py -v
python -m pytest trading/tests/ -q
# Expected: all previous tests + new recovery tests pass
```

---

## Day 2: Order Registry Eviction

### Background

`engine.order_registry` is a `dict[str, tuple[str, Order]]` populated by
`engine.submit_order()` for non-complete orders. It is never cleared for:
- Filled orders (removed from book but kept in registry for trade lookup)
- Rejected market orders

Over time this grows without bound. Goal: evict terminal orders (FILLED, CANCELED,
REJECTED) after a configurable TTL.

### Step 2.1 — Design (no code)

Eviction strategy options:

**Option A: Time-based sweep (chosen)**
Add a timestamp to each registry entry. A periodic background task sweeps entries
older than `TTL` that are in a terminal state. Simple, no hot-path cost.

**Option B: Lazy eviction on access**
Check age on every `get()`. Simpler but misses entries never accessed again.

**Option C: Evict immediately on completion**
Remove from registry when `record_order_complete()` or `record_cancel()` is called.
Breaks trade event lookup in consumer (buyer/seller account lookup happens after fill).

Chosen: **Option A** — periodic sweep at startup and in the snapshot loop.

TTL: 1 hour (configurable). Terminal = FILLED, CANCELED, REJECTED.

New fields needed in registry:
```python
# Before: Dict[str, tuple[str, Order]]
# After:  Dict[str, tuple[str, Order, datetime]]
```

Or keep the tuple unchanged and track timestamps separately:
```python
self._registry_timestamps: Dict[str, datetime] = {}
```

Cleaner: keep the existing registry type, add a parallel `_registry_timestamps` dict
in `MatchingEngine`. This avoids changing the tuple unpacking in all callers.

### Step 2.2 — Update `MatchingEngine` (`trading/engine/matcher.py`)

Changes:
1. Add `_registry_timestamps: Dict[str, datetime] = {}` to `__init__`
2. In `submit_order()`: when adding to `order_registry`, also record
   `self._registry_timestamps[order.order_id] = datetime.now(tz=timezone.utc)`
3. Add `evict_stale_registry(ttl_seconds: int = 3600) -> int` method that:
   - Iterates `order_registry`
   - Removes entries where order is terminal AND age > TTL
   - Returns count of evicted entries
4. Import `datetime`, `timezone` at top of file

```python
def evict_stale_registry(self, ttl_seconds: int = 3600) -> int:
    """
    Remove terminal orders from the registry older than ttl_seconds.

    Terminal = FILLED, CANCELED, REJECTED.
    Returns the number of entries evicted.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=ttl_seconds)
    to_evict = [
        order_id
        for order_id, (ticker, order) in self.order_registry.items()
        if order.is_complete()
        and self._registry_timestamps.get(order_id, datetime.max) < cutoff  # type: ignore[arg-type]
        # datetime.max has no tzinfo — use timezone.utc.localize or replace
    ]
    for order_id in to_evict:
        del self.order_registry[order_id]
        self._registry_timestamps.pop(order_id, None)
    return len(to_evict)
```

**Note on datetime.max**: use `datetime.min.replace(tzinfo=timezone.utc)` as the
fallback, or add `from datetime import datetime, timezone, timedelta` imports.

### Step 2.3 — Wire into periodic snapshot task (`main.py`)

Add `engine.evict_stale_registry()` call inside `_periodic_snapshot`:

```python
async def _periodic_snapshot(
    engine: MatchingEngine,
    event_log: EventLog,
    snapshot_mgr: SnapshotManager,
    interval_seconds: int = 300,
) -> None:
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            engine.evict_stale_registry()
            await snapshot_mgr.save(engine, event_log._sequence)
        except asyncio.CancelledError:
            break
```

Also call once at startup after replay (before consumer starts):
```python
engine.evict_stale_registry()
logger.info("Initial registry eviction complete")
```

### Step 2.4 — Write tests (`trading/tests/test_registry_eviction.py`)

```
test_fresh_terminal_order_not_evicted
  - Submit buy + matching sell → order filled
  - Call evict_stale_registry(ttl_seconds=3600)
  - Registry still has the entry (not old enough)

test_stale_terminal_order_is_evicted
  - Submit and fill an order
  - Manually set its timestamp back >1 hour in _registry_timestamps
  - Call evict_stale_registry(ttl_seconds=3600)
  - Registry entry is gone

test_open_order_never_evicted
  - Submit a resting LIMIT order (no match, stays open)
  - Set timestamp back >1 hour
  - Call evict_stale_registry()
  - Registry entry still present (order is not terminal)

test_evict_returns_count
  - Fill two orders
  - Backdate both timestamps
  - evict_stale_registry() → returns 2

test_cancel_does_not_affect_registry_immediately
  - Cancellation removes from registry in cancel_order() already
  - Verify registry is empty after cancel (existing behaviour)
```

Run:
```bash
python -m pytest trading/tests/test_registry_eviction.py -v
python -m pytest trading/tests/ -q
```

---

## Day 3: Persistent Idempotency

### Background

`IdempotencyStore._cache` is an in-memory dict. After restart it is empty, so
previously seen `order_id` keys are forgotten — a retry after a crash creates a
duplicate order.

Goal: persist the cache so it survives restarts.

### Step 3.1 — Design (no code)

**Persistence options:**

**Option A: New event type `idempotency_cached` in event log**
- Append one line per cached response after each new order
- On startup replay, reconstruct cache by reading these events
- Pro: single source of truth (event log), no new file
- Con: event log grows with idempotency entries; replay loop must skip them for
  engine/risk rebuild

**Option B: Separate flat file `data/idempotency.json`**
- Write full cache dict to JSON on every new entry (or on shutdown)
- Load on startup, merge into `IdempotencyStore`
- Pro: clean separation from event log
- Con: another file to manage; write-on-every-entry may be slow at high throughput

**Option C: Store in snapshot**
- Include `idempotency_cache` dict in `data/snapshot.json`
- Pro: no new file, already reading snapshot at startup
- Con: cache is only as fresh as the last snapshot (entries between snapshot and
  shutdown are lost)

Chosen: **Option A** — append to event log. Consistent with the event-sourced
design. Add a `24-hour TTL` to avoid unbounded growth.

New event type:
```json
{
  "event": "idempotency_cached",
  "seq": <int>,
  "ts": <ISO8601>,
  "order_id": <str>,
  "response": { ...OrderResponse fields... },
  "expires_at": <ISO8601>   // ts + 24h
}
```

### Step 3.2 — Add `append_idempotency_cached` to `EventLog`

In `trading/persistence/event_log.py`:

```python
async def append_idempotency_cached(
    self, order_id: str, response: dict, ttl_hours: int = 24
) -> int:
    """Log a cached idempotency response. Returns the sequence number assigned."""
    from datetime import timedelta
    seq = self._next_seq()
    expires_at = (datetime.now(tz=timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
    await self._write({
        "event": "idempotency_cached",
        "seq": seq,
        "ts": _now(),
        "order_id": order_id,
        "response": response,
        "expires_at": expires_at,
    })
    return seq
```

### Step 3.3 — Update `IdempotencyStore` with TTL and expiry

In `trading/api/dependencies.py`, update `IdempotencyStore`:

```python
class IdempotencyStore:
    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}
        self._expires_at: dict[str, datetime] = {}

    def get(self, order_id: str) -> dict | None:
        if order_id in self._expires_at:
            if datetime.now(tz=timezone.utc) > self._expires_at[order_id]:
                # Expired — treat as unseen
                del self._cache[order_id]
                del self._expires_at[order_id]
                return None
        return self._cache.get(order_id)

    def store(self, order_id: str, response: dict, ttl_hours: int = 24) -> None:
        from datetime import timedelta
        self._cache[order_id] = response
        self._expires_at[order_id] = (
            datetime.now(tz=timezone.utc) + timedelta(hours=ttl_hours)
        )

    def restore(self, order_id: str, response: dict, expires_at: str) -> None:
        """Restore a cache entry from the event log (used during replay)."""
        from datetime import datetime
        expiry = datetime.fromisoformat(expires_at)
        if datetime.now(tz=timezone.utc) < expiry:
            self._cache[order_id] = response
            self._expires_at[order_id] = expiry
        # If already expired, silently drop (no-op)

    def __contains__(self, order_id: str) -> bool:
        return self.get(order_id) is not None
```

### Step 3.4 — Update `submit_order` route to persist on new orders

In `trading/api/routes.py`, after `idempotency.store(...)`:

```python
# Persist to event log for restart recovery
if request.order_id is not None:
    idempotency.store(request.order_id, response.model_dump(mode="json"))
    await event_log.append_idempotency_cached(
        request.order_id, response.model_dump(mode="json")
    )
```

This requires injecting `event_log` into `submit_order`:
```python
async def submit_order(
    request: OrderRequest,
    engine: MatchingEngine = Depends(get_engine),
    queue: asyncio.Queue = Depends(get_order_queue),
    idempotency: IdempotencyStore = Depends(get_idempotency_store),
    event_log: EventLog = Depends(get_event_log),
) -> OrderResponse | JSONResponse:
```

### Step 3.5 — Rebuild idempotency cache on replay (`main.py`)

In `_replay_event`, add a case for `idempotency_cached`:

```python
def _replay_event(
    engine: MatchingEngine,
    event: dict,
    idempotency: IdempotencyStore | None = None,
) -> None:
    if event["event"] == "order_submitted":
        ...  # existing
    elif event["event"] == "order_cancelled":
        ...  # existing
    elif event["event"] == "idempotency_cached" and idempotency is not None:
        idempotency.restore(
            order_id=event["order_id"],
            response=event["response"],
            expires_at=event["expires_at"],
        )
```

Update the lifespan replay loop to pass idempotency:
```python
async for event in event_log.read_all(after_sequence=replay_after):
    _replay_event(engine, event, idempotency=idempotency_store)
```

Retrieve `idempotency_store` from the module-level singleton via `get_idempotency_store()`.

### Step 3.6 — Write tests (`trading/tests/test_idempotency_persistence.py`)

```
test_cached_response_survives_restart
  - Submit order with order_id="persist-1"
  - Restart (second LifespanManager)
  - Submit same order again → expect HTTP 200 with same order_id

test_expired_entry_not_restored
  - Submit order with order_id="expire-1"
  - Manually rewrite the event log entry with expires_at in the past
  - Restart
  - Submit same order again → expect HTTP 201 (cache miss, new order created)

test_no_order_id_nothing_persisted
  - Submit order without order_id
  - Restart
  - Confirm nothing was added to idempotency section of event log

test_idempotency_event_not_replayed_to_engine
  - Submit order with order_id
  - Restart
  - Confirm book only has one order (idempotency_cached event not re-submitted to engine)
```

Run:
```bash
python -m pytest trading/tests/test_idempotency_persistence.py -v
python -m pytest trading/tests/ -q
```

---

## Day 4: Per-Ticker Consumers

### Background

All tickers share one `asyncio.Queue` and one consumer coroutine. A slow match on
AAPL delays MSFT, GOOGL, etc. Goal: shard the queue by ticker so each ticker has
its own consumer and they run concurrently.

### Step 4.1 — Design (no code)

**Architecture change:**

```
Before:
  POST /orders  →  single queue  →  single consumer
                                       ↓
                                  engine (all tickers)

After:
  POST /orders  →  route picks ticker queue
                   queue["AAPL"]  →  consumer["AAPL"]
                   queue["MSFT"]  →  consumer["MSFT"]
                                       ↓
                                  engine (shared)
```

**Shared state concerns:**
- `MatchingEngine` is shared — each consumer writes to a different `OrderBook`.
  `OrderBookManager` routes by ticker so there is no cross-ticker contention in the
  engine itself.
- `RiskChecker` is shared — `record_fill()`, `record_open_order()` etc. update
  `_positions` and `_open_orders` which are cross-ticker per account. Concurrent
  consumers writing to risk state is a data race.

**Risk state concurrency options:**

Option A: One `RiskChecker` per ticker (no sharing) — wrong, position limit is
per (account, ticker) but notional exposure is per account across all tickers.

Option B: `asyncio.Lock` around all risk state mutations — simplest, minimal latency
impact (risk checks are fast).

Option C: Route orders to a per-account consumer instead of per-ticker — complex.

Chosen: **Option B** — add `asyncio.Lock` to `RiskChecker` methods.

**`dependencies.py` change:**
```python
# Before:
_order_queue: asyncio.Queue | None = None
# After:
_order_queues: dict[str, asyncio.Queue] | None = None
```

`init_app_state()` creates one queue per supported ticker:
```python
_order_queues = {ticker: asyncio.Queue() for ticker in SUPPORTED_TICKERS}
```

**Route change (`routes.py`):**
```python
# Before:
queue: asyncio.Queue = Depends(get_order_queue)
await queue.put((order, future))

# After:
queues: dict[str, asyncio.Queue] = Depends(get_order_queues)
await queues[order.ticker].put((order, future))
```

**Consumer change:**
`run_consumer` signature unchanged — still takes a single `asyncio.Queue`. `main.py`
spawns one consumer task per ticker, passing the ticker's queue.

**Lifespan change (`main.py`):**
```python
# Before:
consumer_task = asyncio.create_task(
    consumer.run_consumer(engine, queue, broadcaster, risk, event_log)
)

# After:
consumer_tasks = [
    asyncio.create_task(
        consumer.run_consumer(engine, queues[ticker], broadcaster, risk, event_log),
        name=f"order-consumer-{ticker}",
    )
    for ticker in engine.get_supported_tickers()
]
```

### Step 4.2 — Add `asyncio.Lock` to `RiskChecker`

In `trading/risk/checker.py`:

```python
import asyncio

class RiskChecker:
    def __init__(self, ...):
        ...
        self._lock = asyncio.Lock()

    async def check(self, order: Order) -> None:
        """Async version with lock."""
        async with self._lock:
            self._check_sync(order)

    def _check_sync(self, order: Order) -> None:
        """Original synchronous check logic (unchanged)."""
        ...
```

**Problem:** `consumer.py` currently calls `risk.check(order)` synchronously. After
this change it must `await risk.check(order)`. All consumer callers become async.

Also lock `record_open_order`, `record_fill`, `record_cancel`, `record_order_complete`.

Evaluate carefully: since Python's `asyncio` is single-threaded, an `asyncio.Lock`
only prevents concurrent coroutines from interleaving during an `await`. Since
`RiskChecker` methods have no `await` internally, they cannot be interleaved even
without a lock. However, adding the lock is correct practice for when internal
async I/O is added later, and documents the intent clearly.

**Alternative (simpler):** Keep all risk methods synchronous and rely on the fact
that asyncio is cooperative. Two consumer coroutines can only run concurrently if one
awaits something, and the risk methods never await — so they are implicitly safe.

Document this clearly in comments and skip the lock for now. Revisit if threading is
introduced.

### Step 4.3 — Update `dependencies.py`

1. Rename `_order_queue` → `_order_queues: dict[str, asyncio.Queue] | None`
2. Rename `get_order_queue()` → `get_order_queues()` returning the dict
3. Update `init_app_state()` to create one queue per ticker

### Step 4.4 — Update `routes.py`

1. Inject `queues: dict[str, asyncio.Queue] = Depends(get_order_queues)`
2. Use `queues[order.ticker].put(...)` instead of `queue.put(...)`

### Step 4.5 — Update `main.py`

1. Spawn one consumer task per ticker
2. Shutdown: cancel all consumer tasks, await all
3. Remove single-queue variable, use queues dict

### Step 4.6 — Write tests

No new test file needed — all existing tests exercise the full API and implicitly
test per-ticker routing. Check:

```bash
python -m pytest trading/tests/ -q
# All 171+ tests must still pass
```

Add one explicit test to `test_api.py` or a new file:

```
test_concurrent_orders_on_different_tickers_do_not_interfere
  - POST AAPL BUY, POST MSFT SELL, GET /book/AAPL, GET /book/MSFT
  - Each book has exactly one order
  - No cross-contamination
```

---

## Day 5: Full Test Suite & Validation

### Step 5.1 — Run full suite

```bash
python -m pytest trading/tests/ -v
# Expected: all tests pass (exact count depends on new tests added)
```

### Step 5.2 — Run mypy

```bash
mypy trading/ --ignore-missing-imports
# Expected: no errors
```

### Step 5.3 — Run ruff and black

```bash
ruff check trading/ main.py
black trading/ main.py --check
# Fix any issues, then:
black trading/ main.py
ruff check trading/ main.py --fix
```

### Step 5.4 — Restart smoke test

```bash
uvicorn main:app --port 8000
```

```bash
# 1. Submit order with order_id
curl -si -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"order_id":"w4-smoke","ticker":"AAPL","side":"BUY","order_type":"LIMIT","quantity":9000,"price":"100.00"}' \
  -w "\nHTTP %{http_code}\n"
# Expected: HTTP 201

# 2. Check event log has order_submitted AND idempotency_cached
cat data/events.log | python -m json.tool

# Stop server (Ctrl+C) — final snapshot written

# 3. Restart
uvicorn main:app --port 8000

# 4. Duplicate submission — should return 200 (cache survived restart)
curl -si -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"order_id":"w4-smoke","ticker":"AAPL","side":"BUY","order_type":"LIMIT","quantity":9000,"price":"100.00"}' \
  -w "\nHTTP %{http_code}\n"
# Expected: HTTP 200

# 5. Try order that would exceed position limit
curl -si -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"ticker":"AAPL","side":"BUY","order_type":"LIMIT","quantity":2000,"price":"100.00"}' \
  -w "\nHTTP %{http_code}\n"
# Expected: HTTP 422 "Position limit exceeded" (risk state rebuilt from replay)
```

### Step 5.5 — Run test suite one final time

```bash
python -m pytest trading/tests/ -q
# Record exact test count for summary
```

---

## Day 6: Documentation & Commit

### Step 6.1 — Code formatting (manual)

```bash
black trading/ main.py
ruff check trading/ main.py --fix
```

### Step 6.2 — Type checking (manual)

```bash
mypy trading/ --ignore-missing-imports
# Expected: no errors
```

### Step 6.3 — Smoke test (manual, same as Step 5.4)

Verify all 5 smoke test checks pass.

### Step 6.4 — Write `week4_summary.md`

Follow the same structure as `week3_summary.md`:
- What was built (4 components)
- Updated request flow diagram
- Updated test count table
- Design decisions (per-ticker consumers, risk lock choice, idempotency event type)
- Known limitations (what is still not done)
- Resume bullet

### Step 6.5 — Commit

```bash
git add trading/risk/checker.py
git add trading/engine/matcher.py
git add trading/persistence/event_log.py
git add trading/api/dependencies.py
git add trading/api/routes.py
git add main.py
git add trading/tests/test_recovery.py
git add trading/tests/test_registry_eviction.py
git add trading/tests/test_idempotency_persistence.py
git add week4_execution.md
git add week4_summary.md
git commit -m "..."
```

---

## Troubleshooting

### "Two LifespanManager blocks share module globals"
`init_app_state()` writes to module-level `_engine`, `_risk`, etc. When the first
lifespan exits and the second starts, `init_app_state()` overwrites them cleanly —
this is correct and safe. The globals are reset on each lifespan start.

### "trade_executed account lookup returns None after restart"
`engine.order_registry` only contains resting (non-complete) orders after replay.
Fully-filled orders were removed during `submit_order()` replay. Their accounts
cannot be recovered without storing account_id in the `trade_executed` event.

Quick fix (Day 1 scope): log `buyer_account` and `seller_account` directly in the
`trade_executed` event. Update `EventLog.append_trade_executed` to accept them,
update `consumer.py` to pass them, update `_rebuild_risk` to read them directly.

### "asyncio.Lock in RiskChecker causes coroutine/sync mismatch"
If you add `async def check()` to RiskChecker, update all call sites in consumer.py
to `await risk.check(order)`. Run mypy to catch any missed sites.

### "test_recovery tests are flaky"
The most common cause is test ordering — a previous test writing to `data/events.log`
bleeds into a recovery test. Confirm that `monkeypatch.setenv("EVENT_LOG_PATH", ...)`
is set before `LifespanManager` is created. The env var must be set before
`EventLog.__init__` runs.

---

## Dependency Graph

```
Day 1 (Risk Recovery)
  └── independent — only touches main.py + new test file

Day 2 (Registry Eviction)
  └── independent — only touches matcher.py + main.py + new test file

Day 3 (Persistent Idempotency)
  ├── depends on Day 1 (uses _replay_event with idempotency parameter)
  └── touches event_log.py, dependencies.py, routes.py, main.py

Day 4 (Per-Ticker Consumers)
  └── depends on Day 1 and Day 3 being complete (shared risk + idempotency state)
      touches dependencies.py, routes.py, main.py, consumer.py (risk lock)

Day 5 (Validation)
  └── depends on Days 1-4

Day 6 (Docs & Commit)
  └── depends on Day 5
```

Days 1 and 2 are fully independent and can be done in any order.
Day 3 depends on Day 1 (same `_replay_event` extension).
Day 4 is the most disruptive and should be done last.
