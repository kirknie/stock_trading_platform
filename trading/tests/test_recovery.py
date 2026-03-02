"""
Tests for risk state recovery across restarts.

Verifies that RiskChecker._positions and RiskChecker._open_orders are
correctly rebuilt from the event log after a simulated restart.

Pattern for each test:
  1. First LifespanManager: submit orders, generate state
  2. Exit lifespan (shutdown writes final snapshot)
  3. Second LifespanManager: same tmp_path env vars → replays log
  4. Verify risk limits are enforced as if no restart occurred
"""

import json
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
from main import app

from trading.risk.checker import MAX_POSITION_QUANTITY

# ── Fixture ───────────────────────────────────────────────────────────────────


def make_limit_order(
    ticker: str = "AAPL",
    side: str = "BUY",
    quantity: int = 100,
    price: str = "100.00",
    account_id: str = "default",
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


# ── Position limit rebuilt from trade_executed events ─────────────────────────


async def test_position_limit_enforced_after_restart(tmp_path, monkeypatch):
    """
    Fill 9,000 shares for acc1, restart, then try to add 2,000 more.
    Without recovery the limit would not trigger (position looks like 0).
    With recovery it must raise 422.
    """
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))

    # First lifespan: create a fill of 9,000 shares
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            # Resting sell so the buy can match
            await client.post(
                "/orders",
                json=make_limit_order(
                    side="SELL", quantity=9_000, price="100.00", account_id="acc-sell"
                ),
            )
            resp = await client.post(
                "/orders",
                json=make_limit_order(
                    side="BUY", quantity=9_000, price="100.00", account_id="acc1"
                ),
            )
            assert resp.status_code == 201
            assert resp.json()["filled_quantity"] == 9_000

    # Second lifespan: risk state must be rebuilt
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            # 2,000 more would push acc1 to 11,000 > 10,000 limit
            resp = await client.post(
                "/orders",
                json=make_limit_order(
                    side="BUY", quantity=2_000, price="100.00", account_id="acc1"
                ),
            )
            assert resp.status_code == 422
            assert "Position limit exceeded" in resp.json()["detail"]


async def test_position_limit_allows_order_within_remaining_after_restart(
    tmp_path, monkeypatch
):
    """After filling 5,000 shares and restarting, 4,000 more should still pass."""
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            await client.post(
                "/orders",
                json=make_limit_order(
                    side="SELL", quantity=5_000, price="100.00", account_id="acc-sell"
                ),
            )
            await client.post(
                "/orders",
                json=make_limit_order(
                    side="BUY", quantity=5_000, price="100.00", account_id="acc1"
                ),
            )

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/orders",
                json=make_limit_order(
                    side="BUY", quantity=4_000, price="100.00", account_id="acc1"
                ),
            )
            assert resp.status_code == 201


# ── Notional exposure rebuilt from order_registry (resting orders) ────────────


async def test_notional_exposure_enforced_after_restart(tmp_path, monkeypatch):
    """
    Submit a resting LIMIT buy for $900k, restart, then try to add $200k more.
    Without recovery the notional check would pass (exposure looks like $0).
    With recovery it must raise 422.
    """
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            # $900,000 open — within $1M limit
            resp = await client.post(
                "/orders",
                json=make_limit_order(
                    side="BUY", quantity=9_000, price="100.00", account_id="acc1"
                ),
            )
            assert resp.status_code == 201

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            # $200,000 more → total $1,100,000 > $1,000,000
            resp = await client.post(
                "/orders",
                json=make_limit_order(
                    side="BUY", quantity=2_000, price="100.00", account_id="acc1"
                ),
            )
            assert resp.status_code == 422
            assert "Notional exposure limit exceeded" in resp.json()["detail"]


async def test_notional_exposure_freed_after_cancel_and_restart(tmp_path, monkeypatch):
    """
    Submit a $900k order, cancel it, restart — notional exposure must be $0
    so a new $900k order is accepted.
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
                    side="BUY", quantity=9_000, price="100.00", account_id="acc1"
                ),
            )
            order_id = resp.json()["order_id"]
            cancel = await client.post(f"/orders/{order_id}/cancel")
            assert cancel.json()["success"] is True

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            # Cancelled order is not in registry → exposure is $0 → passes
            resp = await client.post(
                "/orders",
                json=make_limit_order(
                    side="BUY", quantity=9_000, price="100.00", account_id="acc1"
                ),
            )
            assert resp.status_code == 201


# ── Positions are per-account ─────────────────────────────────────────────────


async def test_position_rebuilt_independently_per_account(tmp_path, monkeypatch):
    """
    acc1 fills 9,000 shares. acc2 fills 0. After restart acc2 can still buy
    9,000 shares, but acc1 cannot buy 2,000 more.
    """
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            await client.post(
                "/orders",
                json=make_limit_order(
                    ticker="MSFT",
                    side="SELL",
                    quantity=9_000,
                    price="100.00",
                    account_id="acc-sell",
                ),
            )
            await client.post(
                "/orders",
                json=make_limit_order(
                    ticker="MSFT",
                    side="BUY",
                    quantity=9_000,
                    price="100.00",
                    account_id="acc1",
                ),
            )

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            # acc1: at 9,000 — 2,000 more exceeds limit
            bad = await client.post(
                "/orders",
                json=make_limit_order(
                    ticker="MSFT",
                    side="BUY",
                    quantity=2_000,
                    price="100.00",
                    account_id="acc1",
                ),
            )
            assert bad.status_code == 422

            # acc2: at 0 — 9,000 is fine
            good = await client.post(
                "/orders",
                json=make_limit_order(
                    ticker="MSFT",
                    side="BUY",
                    quantity=9_000,
                    price="100.00",
                    account_id="acc2",
                ),
            )
            assert good.status_code == 201


# ── Clean slate (no prior state) ──────────────────────────────────────────────


async def test_fresh_restart_with_no_log_has_zero_risk_state(tmp_path, monkeypatch):
    """
    No event log → risk state is zero after startup (baseline sanity check).
    """
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            # Full limit order should be accepted on a fresh engine
            resp = await client.post(
                "/orders",
                json=make_limit_order(
                    side="BUY",
                    quantity=MAX_POSITION_QUANTITY,
                    price="1.00",
                    account_id="fresh-acc",
                ),
            )
            assert resp.status_code == 201


# ── trade_executed events carry buyer/seller accounts ─────────────────────────


async def test_trade_executed_event_contains_accounts(tmp_path, monkeypatch):
    """
    The trade_executed NDJSON line must include buyer_account and seller_account
    so _rebuild_risk can reconstruct positions without a registry lookup.
    """
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            await client.post(
                "/orders",
                json=make_limit_order(
                    side="SELL", quantity=10, price="150.00", account_id="seller-acc"
                ),
            )
            await client.post(
                "/orders",
                json=make_limit_order(
                    side="BUY", quantity=10, price="150.00", account_id="buyer-acc"
                ),
            )

    log_lines = (tmp_path / "events.log").read_text().strip().splitlines()
    trade_events = [
        json.loads(line)
        for line in log_lines
        if json.loads(line)["event"] == "trade_executed"
    ]
    assert len(trade_events) == 1
    assert trade_events[0]["buyer_account"] == "buyer-acc"
    assert trade_events[0]["seller_account"] == "seller-acc"
