"""
Tests for event log and snapshot persistence.

Covers:
  - append_order_submitted  — correct JSON written to file
  - append_trade_executed   — correct JSON written to file
  - append_order_cancelled  — correct JSON written to file
  - Sequence numbers        — each event gets an incrementing seq
  - Ordering                — multiple appends appear in write order
  - read_all()              — yields events in file order
  - read_all(after_sequence=N) — filters events by sequence
  - Missing file            — read_all() returns empty iterator
  - Market order price      — None serialises as JSON null
  - SnapshotManager.save()  — correct JSON structure on disk
  - SnapshotManager.load()  — reads back saved snapshot
  - SnapshotManager.restore() — rebuilds order book and registry
  - Version mismatch        — unknown version returns None
  - Missing snapshot        — load() returns None
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading.api.dependencies import SUPPORTED_TICKERS
from trading.engine.matcher import MatchingEngine
from trading.events.models import Order, OrderSide, OrderType, Trade
from trading.persistence.event_log import EventLog
from trading.persistence.snapshot import SNAPSHOT_VERSION, SnapshotManager


# ── Helpers ───────────────────────────────────────────────────────────────────


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


def make_trade(trade_id: str = "T-1") -> Trade:
    return Trade(
        trade_id=trade_id,
        ticker="AAPL",
        buyer_order_id="O-1",
        seller_order_id="O-2",
        price=Decimal("150.00"),
        quantity=100,
        timestamp=datetime.now(tz=timezone.utc),
    )


@pytest.fixture
def tmp_log(tmp_path: Path) -> EventLog:
    """EventLog backed by a temporary directory — isolated per test."""
    return EventLog(path=tmp_path / "events.log")


# ── append_order_submitted ────────────────────────────────────────────────────


async def test_append_order_submitted_writes_file(tmp_log: EventLog, tmp_path: Path):
    order = make_order()
    await tmp_log.append_order_submitted(order)

    log_file = tmp_path / "events.log"
    assert log_file.exists()


async def test_append_order_submitted_event_type(tmp_log: EventLog, tmp_path: Path):
    await tmp_log.append_order_submitted(make_order())
    event = json.loads((tmp_path / "events.log").read_text().strip())
    assert event["event"] == "order_submitted"


async def test_append_order_submitted_order_fields(tmp_log: EventLog, tmp_path: Path):
    order = make_order("O-abc")
    await tmp_log.append_order_submitted(order)
    event = json.loads((tmp_path / "events.log").read_text().strip())
    o = event["order"]
    assert o["order_id"] == "O-abc"
    assert o["ticker"] == "AAPL"
    assert o["side"] == "BUY"
    assert o["order_type"] == "LIMIT"
    assert o["quantity"] == 100
    assert o["price"] == "150.00"
    assert o["account_id"] == "acc1"


async def test_append_order_submitted_returns_sequence(tmp_log: EventLog):
    seq = await tmp_log.append_order_submitted(make_order())
    assert seq == 1


# ── append_trade_executed ─────────────────────────────────────────────────────


async def test_append_trade_executed_event_type(tmp_log: EventLog, tmp_path: Path):
    await tmp_log.append_trade_executed(make_trade())
    event = json.loads((tmp_path / "events.log").read_text().strip())
    assert event["event"] == "trade_executed"


async def test_append_trade_executed_trade_fields(tmp_log: EventLog, tmp_path: Path):
    trade = make_trade("T-xyz")
    await tmp_log.append_trade_executed(trade)
    event = json.loads((tmp_path / "events.log").read_text().strip())
    t = event["trade"]
    assert t["trade_id"] == "T-xyz"
    assert t["ticker"] == "AAPL"
    assert t["buyer_order_id"] == "O-1"
    assert t["seller_order_id"] == "O-2"
    assert t["price"] == "150.00"
    assert t["quantity"] == 100


async def test_append_trade_executed_returns_sequence(tmp_log: EventLog):
    seq = await tmp_log.append_trade_executed(make_trade())
    assert seq == 1


# ── append_order_cancelled ────────────────────────────────────────────────────


async def test_append_order_cancelled_event_type(tmp_log: EventLog, tmp_path: Path):
    await tmp_log.append_order_cancelled("O-1", "AAPL")
    event = json.loads((tmp_path / "events.log").read_text().strip())
    assert event["event"] == "order_cancelled"


async def test_append_order_cancelled_fields(tmp_log: EventLog, tmp_path: Path):
    await tmp_log.append_order_cancelled("O-cancel", "MSFT")
    event = json.loads((tmp_path / "events.log").read_text().strip())
    assert event["order_id"] == "O-cancel"
    assert event["ticker"] == "MSFT"


async def test_append_order_cancelled_returns_sequence(tmp_log: EventLog):
    seq = await tmp_log.append_order_cancelled("O-1", "AAPL")
    assert seq == 1


# ── Sequence numbers ──────────────────────────────────────────────────────────


async def test_sequence_numbers_increment_per_event(tmp_log: EventLog):
    seq1 = await tmp_log.append_order_submitted(make_order("O-1"))
    seq2 = await tmp_log.append_trade_executed(make_trade())
    seq3 = await tmp_log.append_order_cancelled("O-1", "AAPL")
    assert seq1 == 1
    assert seq2 == 2
    assert seq3 == 3


async def test_sequence_numbers_written_to_file(tmp_log: EventLog, tmp_path: Path):
    await tmp_log.append_order_submitted(make_order("O-1"))
    await tmp_log.append_trade_executed(make_trade())

    lines = (tmp_path / "events.log").read_text().strip().splitlines()
    assert json.loads(lines[0])["seq"] == 1
    assert json.loads(lines[1])["seq"] == 2


# ── Ordering ──────────────────────────────────────────────────────────────────


async def test_events_are_appended_in_order(tmp_log: EventLog, tmp_path: Path):
    await tmp_log.append_order_submitted(make_order())
    await tmp_log.append_trade_executed(make_trade())
    await tmp_log.append_order_cancelled("O-2", "MSFT")

    lines = (tmp_path / "events.log").read_text().strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event"] == "order_submitted"
    assert json.loads(lines[1])["event"] == "trade_executed"
    assert json.loads(lines[2])["event"] == "order_cancelled"


# ── read_all ──────────────────────────────────────────────────────────────────


async def test_read_all_yields_events_in_order(tmp_log: EventLog):
    await tmp_log.append_order_submitted(make_order())
    await tmp_log.append_trade_executed(make_trade())

    events = [e async for e in tmp_log.read_all()]
    assert len(events) == 2
    assert events[0]["event"] == "order_submitted"
    assert events[1]["event"] == "trade_executed"


async def test_read_all_returns_empty_for_missing_file(tmp_path: Path):
    log = EventLog(path=tmp_path / "nonexistent.log")
    events = [e async for e in log.read_all()]
    assert events == []


async def test_read_all_after_sequence_filters_events(tmp_log: EventLog):
    await tmp_log.append_order_submitted(make_order("O-1"))
    await tmp_log.append_trade_executed(make_trade("T-1"))
    await tmp_log.append_order_cancelled("O-1", "AAPL")

    # Only events with seq > 1
    events = [e async for e in tmp_log.read_all(after_sequence=1)]
    assert len(events) == 2
    assert events[0]["seq"] == 2
    assert events[1]["seq"] == 3


async def test_read_all_after_sequence_zero_yields_all(tmp_log: EventLog):
    await tmp_log.append_order_submitted(make_order())
    await tmp_log.append_trade_executed(make_trade())

    events = [e async for e in tmp_log.read_all(after_sequence=0)]
    assert len(events) == 2


async def test_read_all_after_sequence_beyond_last_yields_nothing(tmp_log: EventLog):
    await tmp_log.append_order_submitted(make_order())

    events = [e async for e in tmp_log.read_all(after_sequence=99)]
    assert events == []


# ── Edge cases ────────────────────────────────────────────────────────────────


async def test_market_order_price_serialises_as_null(tmp_log: EventLog, tmp_path: Path):
    order = Order(
        order_id="O-mkt",
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


async def test_each_event_has_ts_field(tmp_log: EventLog):
    await tmp_log.append_order_submitted(make_order())
    await tmp_log.append_trade_executed(make_trade())
    await tmp_log.append_order_cancelled("O-1", "AAPL")

    events = [e async for e in tmp_log.read_all()]
    for event in events:
        assert "ts" in event
        # ISO 8601 — parseable by fromisoformat
        datetime.fromisoformat(event["ts"])


async def test_parent_directory_created_automatically(tmp_path: Path):
    nested_path = tmp_path / "a" / "b" / "c" / "events.log"
    log = EventLog(path=nested_path)
    await log.append_order_submitted(make_order())
    assert nested_path.exists()


# ── SnapshotManager helpers ───────────────────────────────────────────────────


def make_engine() -> MatchingEngine:
    return MatchingEngine(SUPPORTED_TICKERS)


def make_resting_order(
    order_id: str = "O-snap",
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


# ── SnapshotManager.save / load ───────────────────────────────────────────────


async def test_save_creates_file(tmp_path: Path):
    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(make_engine(), sequence=0)
    assert (tmp_path / "snapshot.json").exists()


async def test_save_includes_version_and_sequence(tmp_path: Path):
    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(make_engine(), sequence=42)

    data = json.loads((tmp_path / "snapshot.json").read_text())
    assert data["version"] == SNAPSHOT_VERSION
    assert data["sequence"] == 42


async def test_save_includes_all_supported_tickers(tmp_path: Path):
    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(make_engine(), sequence=0)

    data = json.loads((tmp_path / "snapshot.json").read_text())
    assert set(data["books"].keys()) == set(SUPPORTED_TICKERS)


async def test_save_captures_resting_bid(tmp_path: Path):
    engine = make_engine()
    engine.submit_order(make_resting_order("O-1", "AAPL", OrderSide.BUY, "149.00", 100))

    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(engine, sequence=1)

    data = json.loads((tmp_path / "snapshot.json").read_text())
    bids = data["books"]["AAPL"]["bids"]
    assert len(bids) == 1
    assert bids[0]["order_id"] == "O-1"
    assert bids[0]["price"] == "149.00"
    assert bids[0]["quantity"] == 100


async def test_save_captures_resting_ask(tmp_path: Path):
    engine = make_engine()
    engine.submit_order(make_resting_order("O-2", "MSFT", OrderSide.SELL, "310.00", 50))

    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(engine, sequence=1)

    data = json.loads((tmp_path / "snapshot.json").read_text())
    asks = data["books"]["MSFT"]["asks"]
    assert len(asks) == 1
    assert asks[0]["order_id"] == "O-2"


async def test_save_does_not_capture_filled_orders(tmp_path: Path):
    engine = make_engine()
    # Sell rests first, then buy matches it fully
    engine.submit_order(make_resting_order("O-sell", "AAPL", OrderSide.SELL, "150.00", 100))
    engine.submit_order(make_resting_order("O-buy", "AAPL", OrderSide.BUY, "150.00", 100))

    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(engine, sequence=2)

    data = json.loads((tmp_path / "snapshot.json").read_text())
    assert data["books"]["AAPL"]["bids"] == []
    assert data["books"]["AAPL"]["asks"] == []


async def test_load_returns_none_for_missing_file(tmp_path: Path):
    mgr = SnapshotManager(path=tmp_path / "no_snapshot.json")
    assert await mgr.load() is None


async def test_load_returns_none_for_wrong_version(tmp_path: Path):
    snap_path = tmp_path / "snapshot.json"
    snap_path.write_text(json.dumps({"version": 99, "sequence": 0, "books": {}}))

    mgr = SnapshotManager(path=snap_path)
    assert await mgr.load() is None


async def test_load_roundtrip(tmp_path: Path):
    engine = make_engine()
    engine.submit_order(make_resting_order("O-rt", "AAPL", OrderSide.BUY, "148.00", 75))

    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(engine, sequence=5)

    snapshot = await mgr.load()
    assert snapshot is not None
    assert snapshot["sequence"] == 5
    assert snapshot["books"]["AAPL"]["bids"][0]["order_id"] == "O-rt"


# ── SnapshotManager.restore ───────────────────────────────────────────────────


async def test_restore_rebuilds_best_bid(tmp_path: Path):
    engine1 = make_engine()
    engine1.submit_order(make_resting_order("O-1", "AAPL", OrderSide.BUY, "149.00", 100))

    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(engine1, sequence=1)

    engine2 = make_engine()
    snapshot = await mgr.load()
    mgr.restore(engine2, snapshot)

    book = engine2.manager.get_order_book("AAPL")
    assert book.get_best_bid() == Decimal("149.00")


async def test_restore_rebuilds_best_ask(tmp_path: Path):
    engine1 = make_engine()
    engine1.submit_order(make_resting_order("O-1", "AAPL", OrderSide.SELL, "151.00", 50))

    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(engine1, sequence=1)

    engine2 = make_engine()
    mgr.restore(engine2, await mgr.load())

    book = engine2.manager.get_order_book("AAPL")
    assert book.get_best_ask() == Decimal("151.00")


async def test_restore_populates_order_registry(tmp_path: Path):
    engine1 = make_engine()
    engine1.submit_order(make_resting_order("O-reg", "AAPL", OrderSide.BUY, "148.00", 30))

    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(engine1, sequence=1)

    engine2 = make_engine()
    mgr.restore(engine2, await mgr.load())

    assert "O-reg" in engine2.order_registry
    ticker, order = engine2.order_registry["O-reg"]
    assert ticker == "AAPL"
    assert order.quantity == 30


async def test_restore_multiple_price_levels(tmp_path: Path):
    engine1 = make_engine()
    engine1.submit_order(make_resting_order("O-1", "AAPL", OrderSide.BUY, "149.00", 100))
    engine1.submit_order(make_resting_order("O-2", "AAPL", OrderSide.BUY, "148.00", 200))

    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(engine1, sequence=2)

    engine2 = make_engine()
    mgr.restore(engine2, await mgr.load())

    book = engine2.manager.get_order_book("AAPL")
    assert book.get_best_bid() == Decimal("149.00")
    assert len(book.bids) == 2


async def test_restore_empty_snapshot_leaves_engine_empty(tmp_path: Path):
    mgr = SnapshotManager(path=tmp_path / "snapshot.json")
    await mgr.save(make_engine(), sequence=0)

    engine2 = make_engine()
    mgr.restore(engine2, await mgr.load())

    book = engine2.manager.get_order_book("AAPL")
    assert book.get_best_bid() is None
    assert book.get_best_ask() is None
    assert engine2.order_registry == {}
