"""
Tests for persistent idempotency cache across restarts.

Verifies that idempotency_cached events written to the event log are
correctly restored into IdempotencyStore on startup, and that the TTL
and edge cases (no order_id, expired entries) work correctly.

Pattern for restart tests:
  1. First LifespanManager: submit orders, write idempotency_cached events
  2. Exit lifespan (shutdown writes final snapshot)
  3. Second LifespanManager: same tmp_path env vars → replays log
  4. Verify cache state matches expectations
"""

import json
from datetime import datetime, timedelta, timezone

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from main import app

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_limit_order(
    ticker: str = "AAPL",
    side: str = "BUY",
    quantity: int = 10,
    price: str = "100.00",
    account_id: str = "acc1",
    order_id: str | None = None,
) -> dict:
    payload: dict = {
        "ticker": ticker,
        "side": side,
        "order_type": "LIMIT",
        "quantity": quantity,
        "price": price,
        "account_id": account_id,
    }
    if order_id is not None:
        payload["order_id"] = order_id
    return payload


# ── Cached response survives restart ─────────────────────────────────────────


async def test_cached_response_survives_restart(tmp_path, monkeypatch):
    """
    Submit an order with a client order_id, restart, then submit the same
    order_id again — must get HTTP 200 with the original response.
    """
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/orders", json=make_limit_order(order_id="persist-1")
            )
            assert resp.status_code == 201
            original_order_id = resp.json()["order_id"]

    # Second lifespan: cache must be rebuilt from event log
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/orders", json=make_limit_order(order_id="persist-1")
            )
            assert resp.status_code == 200  # replay, not new order
            assert resp.json()["order_id"] == original_order_id


# ── Expired entry is not restored ─────────────────────────────────────────────


async def test_expired_entry_not_restored(tmp_path, monkeypatch):
    """
    Submit an order, then backdate the expires_at in the event log to the past.
    After restart the entry must not be in the cache — the same order_id should
    create a new order (HTTP 201).
    """
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))
    log_path = tmp_path / "events.log"

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/orders", json=make_limit_order(order_id="expire-1")
            )
            assert resp.status_code == 201

    # Rewrite the idempotency_cached event with an expires_at in the past
    lines = log_path.read_text().strip().splitlines()
    rewritten = []
    for line in lines:
        event = json.loads(line)
        if event.get("event") == "idempotency_cached":
            event["expires_at"] = (
                datetime.now(tz=timezone.utc) - timedelta(hours=1)
            ).isoformat()
        rewritten.append(json.dumps(event))
    log_path.write_text("\n".join(rewritten) + "\n")

    # Second lifespan: expired entry must be dropped, new order created
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/orders", json=make_limit_order(order_id="expire-1")
            )
            assert resp.status_code == 201  # cache miss — new order


# ── No order_id means no idempotency event written ───────────────────────────


async def test_no_order_id_nothing_persisted(tmp_path, monkeypatch):
    """
    Submitting without a client order_id must not write any idempotency_cached
    event to the log.
    """
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))
    log_path = tmp_path / "events.log"

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.post("/orders", json=make_limit_order())
            assert resp.status_code == 201

    idempotency_events = [
        json.loads(line)
        for line in log_path.read_text().strip().splitlines()
        if json.loads(line).get("event") == "idempotency_cached"
    ]
    assert idempotency_events == []


# ── idempotency_cached event not replayed to engine ──────────────────────────


async def test_idempotency_event_not_replayed_to_engine(tmp_path, monkeypatch):
    """
    The idempotency_cached event must not cause a second order to be submitted
    to the engine on replay. After restart the order book must have exactly the
    same state as before (one resting bid, not two).
    """
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/orders",
                json=make_limit_order(
                    ticker="AAPL", side="BUY", price="100.00", order_id="eng-test-1"
                ),
            )
            assert resp.status_code == 201

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            book = await client.get("/book/AAPL")
            bids = book.json()["bids"]
            # Exactly one resting bid — not duplicated by idempotency replay
            assert len(bids) == 1
            assert bids[0]["quantity"] == 10


# ── Duplicate submission returns original response fields ────────────────────


async def test_cached_response_has_correct_fields(tmp_path, monkeypatch):
    """
    The replayed cached response must contain the same order_id, status, and
    filled_quantity as the original.
    """
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            first = await client.post(
                "/orders", json=make_limit_order(order_id="fields-1")
            )
            assert first.status_code == 201
            first_body = first.json()

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            second = await client.post(
                "/orders", json=make_limit_order(order_id="fields-1")
            )
            assert second.status_code == 200
            second_body = second.json()

    assert second_body["order_id"] == first_body["order_id"]
    assert second_body["status"] == first_body["status"]
    assert second_body["filled_quantity"] == first_body["filled_quantity"]
