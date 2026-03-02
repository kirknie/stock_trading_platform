# Week 4 Summary: Resilience & Scalability

## What Was Built

Risk state recovery across restarts, order registry eviction, persistent idempotency cache,
per-ticker consumers, and a sequence continuity fix — making the platform durable through
crashes and horizontally partitioned by ticker for concurrent order processing.

---

## Core Components

### 1. Risk State Recovery (`main.py`)

After restart, `RiskChecker` state is rebuilt from the event log in two phases:

- **Phase 1** — `_open_orders`: iterate `engine.order_registry` after engine replay; every
  resting LIMIT order is an open exposure entry.
- **Phase 2** — `_positions`: scan all `trade_executed` events from seq=0; `buyer_account`
  and `seller_account` are stored directly in each event so no registry lookup is needed.

`_rebuild_risk()` is called once at startup after `_replay_event` has reconstructed the
engine, before the consumer task starts.

**Key design change:** `append_trade_executed()` was extended to accept `buyer_account` and
`seller_account` as explicit parameters, written alongside the trade in the event. This
decouples recovery from the order registry (which only holds resting orders post-replay).

**Race condition fixed:** `future.set_result(trades)` in `consumer.py` was moved to after
all trade event logging and risk state updates. Previously the HTTP response could be
returned before the log writes completed, meaning a crash between response and log write
would leave trades unrecorded.

### 2. Order Registry Eviction (`trading/engine/matcher.py`)

`engine.order_registry` previously grew without bound — filled makers stayed in the registry
forever after their counterparty was matched.

- `_registry_timestamps: Dict[str, datetime]` — parallel dict recording when each order
  entered the registry
- `evict_stale_registry(ttl_seconds=3600)` — removes terminal orders (`is_complete()`) whose
  timestamp is older than the TTL; returns eviction count
- Called once at startup (after replay) and on every periodic snapshot cycle

**Key insight:** only *maker* (resting) orders enter the registry — aggressors that fully
fill on arrival are never registered (`not order.is_complete()` guard in `submit_order()`).
Eviction targets only these maker entries once they are terminal and stale.

### 3. Persistent Idempotency (`trading/persistence/event_log.py`, `trading/api/dependencies.py`)

`IdempotencyStore._cache` previously reset to empty on every restart.

- New event type `idempotency_cached` appended to the event log on first submission of a
  client `order_id`; carries `order_id`, `response`, and `expires_at` (now + 24h)
- `IdempotencyStore.restore()` replays these events at startup, silently dropping expired
  entries
- `_rebuild_idempotency()` in `main.py` always reads from seq=0 — mirrors `_rebuild_risk`,
  since the snapshot does not capture idempotency state
- TTL expiry added to `IdempotencyStore.get()`: lazy eviction on access

**Snapshot vs full-replay asymmetry:** the engine is rebuilt from snapshot + delta; risk and
idempotency are always rebuilt from the full log (seq=0) because neither is captured in the
snapshot.

### 4. Per-Ticker Consumers (`trading/api/dependencies.py`, `main.py`, `trading/api/routes.py`)

All tickers previously shared one `asyncio.Queue` and one consumer task. A slow match on
AAPL would delay MSFT, GOOGL, etc.

- `_order_queues: dict[str, asyncio.Queue]` — one queue per supported ticker
- `main.py` spawns one `run_consumer` task per ticker at startup, named
  `order-consumer-{ticker}`; cancels all on shutdown
- `routes.py` routes each order to `queues[order.ticker]`
- `consumer.py` unchanged — still takes a single `asyncio.Queue`

**Concurrency safety:** `RiskChecker` methods have no internal `await`s, so they cannot be
interleaved by the asyncio scheduler even with multiple concurrent consumers. No
`asyncio.Lock` needed (documented in `main.py`).

### 5. Sequence Continuity Fix (`trading/persistence/event_log.py`)

`EventLog._sequence` was reset to 0 on every startup. After restart, new events would
collide with existing sequence numbers in the log file, causing `replay_after` to be saved
incorrectly in the next snapshot and replaying old events redundantly.

- `_read_max_sequence()` — synchronous scan of the log file at `__init__` time; sets
  `_sequence` to the highest `seq` found
- New events after restart correctly continue from the previous maximum

---

## Request Flow (Updated)

```
POST /orders
    │
    ├─ IdempotencyStore.get(order_id)?  ──YES──→ HTTP 200 (cached, from event log)
    │         NO
    ↓
queues[ticker].put((order, future))
    │
    ↓
run_consumer-{ticker}                    ← one per ticker, concurrent
    ├─ risk.check(order)
    ├─ risk.check_market_spread()        (MARKET only)
    ├─ event_log.append_order_submitted()
    ├─ risk.record_open_order()
    ├─ engine.submit_order()
    ├─ event_log.append_trade_executed() (per trade, with buyer/seller accounts)
    ├─ risk.record_fill()                (per trade)
    ├─ risk.record_order_complete()      (if fully filled)
    └─ future.set_result(trades)
    │
    ↓
IdempotencyStore.store() + event_log.append_idempotency_cached()
    │
    ↓
HTTP 201
```

---

## Startup Sequence (Updated)

```
1. init_app_state()               — create engine, queues, risk, event_log, snapshot_mgr
2. snapshot_mgr.load()            — load latest snapshot (if any)
3. snapshot_mgr.restore()         — rebuild resting orders from snapshot
4. event_log.read_all(seq > N)    — replay engine events after snapshot
   └─ _replay_event()             — re-submit/re-cancel each event
5. _rebuild_risk()                — Phase 1: open orders from registry
                                    Phase 2: positions from trade_executed (seq=0)
6. _rebuild_idempotency()         — restore cache from idempotency_cached (seq=0)
7. engine.evict_stale_registry()  — initial TTL sweep
8. run_consumer-{ticker} × N      — one consumer per ticker
9. _periodic_snapshot()           — snapshot + eviction every 5 minutes
```

---

## Test Coverage

### New Tests Added This Week

| File | Tests | Covers |
|------|-------|--------|
| `test_recovery.py` | 7 | Risk state rebuilt after restart (positions, notional, per-account) |
| `test_registry_eviction.py` | 8 | Stale eviction, TTL, open order immunity, cancel interaction |
| `test_idempotency_persistence.py` | 5 | Cache survives restart, TTL expiry, no-id no-persist, engine isolation |
| `test_persistence.py` | +2 | Sequence continuity after reinit, fresh-file baseline |
| `test_api.py` | +1 | Cross-ticker order isolation (per-ticker queues) |

### Full Suite

| File | Tests |
|------|-------|
| `test_models.py` | 10 |
| `test_order_book.py` | 16 |
| `test_order_book_manager.py` | 14 |
| `test_integration.py` | 13 |
| `test_edge_cases.py` | 19 |
| `test_properties.py` | 10 |
| `test_api.py` | 18 |
| `test_websocket.py` | 5 |
| `test_risk.py` | 21 |
| `test_persistence.py` | 37 |
| `test_idempotency.py` | 11 |
| `test_recovery.py` | 7 |
| `test_registry_eviction.py` | 8 |
| `test_idempotency_persistence.py` | 5 |
| **Total** | **194** |

---

## Design Decisions

### Accounts embedded in `trade_executed` events
`Trade` dataclass does not carry `account_id` — it models an exchange-level execution, not
an account-level event. Embedding `buyer_account`/`seller_account` in the `trade_executed`
event keeps the recovery path independent of the order registry, which only holds resting
orders post-replay. Separation of concerns: `Trade` is a matching primitive; accounts are
a business concern.

### `_rebuild_risk` and `_rebuild_idempotency` always read from seq=0
The snapshot captures engine state (order books, registry) but not risk or idempotency state.
Reading from seq=0 is the correct baseline for both. Reading from `replay_after` would miss
all events that occurred before the snapshot — exactly the events that matter most for
position and cache state. This is consistent with how the snapshot was designed: it replaces
the need to replay order_submitted events, but not trade_executed or idempotency_cached.

### No `asyncio.Lock` on `RiskChecker`
Python's asyncio is cooperative — coroutines only yield at `await` points. All `RiskChecker`
methods are synchronous (no `await`), so they cannot be interleaved by the scheduler even
with N concurrent consumer coroutines. A lock would be correct but have zero contention and
add only overhead. This would need to be revisited if `RiskChecker` ever gains async I/O or
if the system moves to thread-based parallelism.

### Per-ticker queue sharding, not per-account
Per-ticker partitioning is natural: `OrderBook` is already ticker-scoped, and cross-ticker
contention in the engine is structurally impossible (`OrderBookManager` routes by ticker).
Per-account sharding would complicate risk state (notional exposure is per-account across
all tickers) and require a different routing strategy in the route handler.

### Periodic eviction on snapshot cycle, not on every order
Eviction sweeps the registry on every `_periodic_snapshot` call (every 5 minutes). This
amortises the O(n) scan cost across many orders. Evicting on every order completion would
be O(1) but requires knowing when an order becomes terminal in the consumer — which would
couple the eviction concern into the hot path.

---

## Known Limitations

1. **No authentication** — any caller can submit orders for any account.
2. **No tick size enforcement** — any two-decimal-place price is accepted.
3. **Idempotency cache is in-memory with no size cap** — a sustained load of unique order
   IDs could exhaust memory before the 24-hour TTL expires. A production system would cap
   entries and use an LRU eviction policy.
4. **Snapshot does not include risk or idempotency state** — cold start always replays the
   full log for risk and idempotency, which grows with time. A future improvement would
   checkpoint these into the snapshot.
5. **Single event log file** — all tickers share one log; high throughput could make the
   file a write bottleneck. Per-ticker log sharding mirrors the per-ticker queue design.

---

## File Structure

```
trading/
├── api/
│   ├── consumer.py           (+trade logging before future.set_result)
│   ├── dependencies.py       (_order_queues dict, IdempotencyStore TTL + restore)
│   └── routes.py             (queues[ticker], event_log in submit_order)
├── engine/
│   └── matcher.py            (+_registry_timestamps, evict_stale_registry)
├── persistence/
│   ├── event_log.py          (+append_idempotency_cached, _read_max_sequence)
│   └── snapshot.py           (unchanged)
└── tests/
    ├── test_recovery.py              (7 tests, new)
    ├── test_registry_eviction.py     (8 tests, new)
    └── test_idempotency_persistence.py (5 tests, new)

main.py                       (+_rebuild_risk, _rebuild_idempotency, per-ticker consumers)
```

---

## Validation Checklist

- [x] 194 tests pass (`pytest trading/tests/ -q`)
- [x] mypy clean (`mypy trading/ --ignore-missing-imports`)
- [x] Smoke test: risk state enforced after restart (position limit, notional exposure)
- [x] Smoke test: idempotent order returns HTTP 200 after restart
- [x] Smoke test: sequence numbers continue correctly after restart (no collision)
- [x] Per-ticker books fully isolated (AAPL order does not appear in MSFT book)

---

## Resume Bullet

> "Hardened a Python asyncio trading platform for crash recovery: embedded buyer/seller
> accounts in trade events to enable stateless risk rebuilding on restart; added TTL-based
> order registry eviction to cap memory growth; persisted the idempotency cache in the
> append-only event log with 24-hour TTL; sharded the order queue by ticker for concurrent
> matching across symbols; and fixed a sequence counter bug that caused event log collisions
> after restart. Grew the test suite from 171 to 194 tests including full restart simulation
> via sequential LifespanManager blocks."

---

## What's Next: Week 5 (Candidates)

- **Authentication** — API key or JWT for order submission
- **Tick size enforcement** — validate price increments per ticker
- **Snapshot includes risk + idempotency state** — eliminate full log replay on cold start
- **Per-ticker event log sharding** — separate log files to remove write bottleneck
- **Order expiry (GTD / IOC / FOK)** — time-in-force order types
