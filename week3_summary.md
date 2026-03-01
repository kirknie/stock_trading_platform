# Week 3 Summary: Risk, Persistence & Idempotency

## What Was Built

Pre-trade risk checks, an append-only event log, snapshot + replay recovery, and
idempotent order submission — making the platform safe to restart without losing
order book state and safe to retry without creating duplicate orders.

---

## Core Components

### 1. Pre-Trade Risk Checker (`trading/risk/checker.py`)

Three independent checks executed in the consumer before every order reaches the engine:

- **Position limit** — max 10,000 shares per (account, ticker). Tracks filled quantity
  via `record_fill()` and checks `current_position + order_quantity ≤ MAX`.
- **Notional exposure** — max $1,000,000 open value per account. Tracks open LIMIT orders
  via `record_open_order()` / `record_cancel()` / `record_order_complete()`.
- **Market spread protection** — rejects MARKET orders if `spread > MAX_SPREAD ($50)` or
  if both sides of the book are empty.

All violations raise `RiskViolation`, caught by the consumer, set on the future, and
surfaced to the HTTP caller as HTTP 422.

### 2. Append-Only Event Log (`trading/persistence/event_log.py`)

Every domain event is written as a JSON line (NDJSON) to `data/events.log`:

- `order_submitted` — written after risk checks pass, before `submit_order()`
- `trade_executed` — written once per generated trade
- `order_cancelled` — written after a successful cancel

Each event carries a monotonic `seq` integer. `read_all(after_sequence=N)` yields only
events with `seq > N`, enabling efficient partial replay after a snapshot.

Path is configurable via `EVENT_LOG_PATH` env var (used in tests for isolation).

### 3. Snapshot Manager (`trading/persistence/snapshot.py`)

Captures all resting orders from every book into `data/snapshot.json`:

- `save(engine, sequence)` — serialises all bids/asks with the current event log
  sequence number; overwrites the previous file
- `load()` — returns `None` if the file is missing or has a version mismatch
- `restore(engine, snapshot)` — places orders back into books via `OrderBook._add_to_book()`
  (no matching triggered) and repopulates `order_registry`

Path is configurable via `SNAPSHOT_PATH` env var.

### 4. Startup Recovery (`main.py`)

```
1. init_app_state()               — create all singletons
2. snapshot_mgr.load()            — load latest snapshot (if any)
3. snapshot_mgr.restore()         — rebuild resting orders
4. event_log.read_all(seq > N)    — replay events after snapshot
5. _replay_event()                — re-submit/re-cancel each event
6. run_consumer() + _periodic_snapshot()  — start background tasks
```

On shutdown: background tasks cancelled, final snapshot written. Periodic snapshot
runs every 5 minutes during operation.

### 5. Idempotent Order Submission

- `OrderRequest.order_id` — optional client-supplied key (up to 128 chars)
- `IdempotencyStore` — in-memory `dict[str, dict]` mapping key → cached response
- Check at route layer before the order reaches the queue:
  - Key seen before → return cached `OrderResponse` as HTTP **200**
  - New key → process normally, cache response, return HTTP **201**
  - No key → no caching, always creates a new order (HTTP 201)

---

## Request Flow (Updated)

```
POST /orders
    │
    ├─ IdempotencyStore.get(order_id)?  ──YES──→ HTTP 200 (cached)
    │         NO
    ↓
asyncio.Queue  ←──────── handler awaits Future
    │
    ↓
run_consumer
    ├─ risk.check(order)               — position + notional limits
    ├─ risk.check_market_spread()      — spread protection (MARKET only)
    ├─ event_log.append_order_submitted()
    ├─ risk.record_open_order()
    ├─ engine.submit_order()           — matching engine
    ├─ event_log.append_trade_executed()  (per trade)
    ├─ risk.record_fill()                 (per trade)
    ├─ risk.record_order_complete()    — if fully filled
    └─ broadcaster.notify_*()          — WebSocket push
    │
    ↓
future.set_result(trades) ──→ IdempotencyStore.store() ──→ HTTP 201
```

---

## REST Endpoints (Updated)

| Method | Path | Status | Description |
|--------|------|--------|-------------|
| `POST` | `/orders` | 201 / 200 / 422 | Submit order; 200 = idempotency replay |
| `POST` | `/orders/{id}/cancel` | 200 | Cancel open order |
| `GET` | `/book/{ticker}` | 200 / 404 | Order book snapshot |
| `GET` | `/tickers` | 200 | List supported tickers |

---

## Test Coverage

### New Tests Added This Week

| File | Tests | Covers |
|------|-------|--------|
| `test_risk.py` | 21 | Position limits, notional exposure, spread checks, API integration |
| `test_persistence.py` | 35 | EventLog (21) + SnapshotManager save/load/restore (14) |
| `test_idempotency.py` | 11 | Dedup, book integrity, no-key behaviour, key specificity |

### Full Suite

| File | Tests |
|------|-------|
| `test_models.py` | 10 |
| `test_order_book.py` | 16 |
| `test_order_book_manager.py` | 14 |
| `test_integration.py` | 13 |
| `test_edge_cases.py` | 19 |
| `test_properties.py` | 10 |
| `test_api.py` | 17 |
| `test_websocket.py` | 5 |
| `test_risk.py` | 21 |
| `test_persistence.py` | 35 |
| `test_idempotency.py` | 11 |
| **Total** | **171** |

**Key testing patterns:**
- `monkeypatch.setenv("EVENT_LOG_PATH", ...)` + `monkeypatch.setenv("SNAPSHOT_PATH", ...)`
  redirects all I/O to `tmp_path` — every test starts with a clean engine and empty files
- Snapshot tests use two separate `MatchingEngine` instances to verify save → restore
  produces identical book state
- Idempotency tests verify book quantity (not just HTTP status) to confirm no duplicate
  resting orders were created

---

## Design Decisions

### Risk checks in the consumer, not the route
Risk state is mutable (`_positions`, `_open_orders`). Placing checks in the single consumer
coroutine means all reads and writes are serialised — no locks needed. Putting them in the
route handler would require locking because multiple HTTP requests run concurrently.

### Event log written before `submit_order()`
`append_order_submitted()` is called before `engine.submit_order()`. If the process crashes
between the two, the order is in the log but not in the engine. On replay, `submit_order()`
is called again — the order reaches the engine correctly. Writing after would mean a crash
leaves the engine state without a log entry, making recovery impossible.

### Snapshot bypasses matching via `_add_to_book()`
Snapshot orders were already non-matching when saved (they were resting in the book). Using
`add_limit_order()` during restore would re-run matching against them, potentially generating
duplicate trades. `_add_to_book()` places orders directly onto the resting side.

### Idempotency is opt-in, in-memory, no TTL
Clients that need dedup supply `order_id`; others get the original behaviour. The store is
in-memory because persistent idempotency (surviving restarts) would require the idempotency
cache to be part of the event log — a non-trivial addition deferred intentionally.

---

## Known Limitations

1. **Idempotency store does not survive restart** — the in-memory cache is lost on shutdown.
   A production system would persist the cache (e.g. in the event log or Redis with a TTL).
2. **Risk state not rebuilt on replay** — `_positions` and `_open_orders` in `RiskChecker`
   start empty after restart. A future step would replay `trade_executed` events to rebuild
   positions and `order_submitted` + `order_cancelled` to rebuild open notional exposure.
3. **No tick size enforcement** — any two-decimal-place price is accepted.
4. **Order registry grows unbounded** — filled/cancelled orders accumulate in memory
   (not yet evicted).
5. **Single consumer** — all tickers share one consumer; a slow match delays all others.

---

## File Structure

```
trading/
├── api/
│   ├── consumer.py       # 110 lines  (+risk + event_log integration)
│   ├── dependencies.py   # 113 lines  (+RiskChecker, EventLog, SnapshotManager, IdempotencyStore)
│   ├── routes.py         # 237 lines  (+risk cancel, idempotency check)
│   └── schemas.py        # 109 lines  (+order_id field)
├── risk/
│   └── checker.py        # 179 lines  (new)
├── persistence/
│   ├── event_log.py      # 152 lines  (new)
│   └── snapshot.py       # 141 lines  (new)
└── tests/
    ├── test_risk.py          #  21 tests (new)
    ├── test_persistence.py   #  35 tests (new)
    └── test_idempotency.py   #  11 tests (new)

main.py                   # 139 lines  (+snapshot restore, replay, periodic snapshot)

New this week: ~1,180 lines source, 67 new tests
```

---

## Validation Checklist

- [x] 171 tests pass (`pytest trading/tests/ -q`)
- [x] mypy clean (`mypy trading/ --ignore-missing-imports`)
- [x] Smoke test: order submitted, event log written, snapshot saved on shutdown
- [x] Smoke test: after restart, resting order visible in book (snapshot restored)
- [x] Duplicate `order_id` returns HTTP 200 with cached response
- [x] Risk violations return HTTP 422 with descriptive message
- [x] Code formatted and linted (steps 6.1–6.2)

---

## Resume Bullet (Draft)

> "Extended a deterministic limit order book with pre-trade risk controls (position limits,
> notional exposure, spread protection), an append-only NDJSON event log, and snapshot +
> replay crash recovery, reducing cold-start replay cost proportionally to snapshot
> frequency. Added opt-in idempotent order submission returning cached responses for
> duplicate client keys. Grew the test suite to 171 tests with full I/O isolation via
> pytest tmp_path."

---

## What's Next: Week 4 (Candidates)

- **Risk state recovery on replay** — rebuild `RiskChecker` positions from event log
- **Persistent idempotency** — survive restarts by persisting the idempotency cache
- **Per-ticker consumers** — shard the queue by ticker to remove the single-consumer bottleneck
- **Order registry eviction** — remove terminal orders from memory on a TTL
- **Authentication** — API key or JWT for order submission
