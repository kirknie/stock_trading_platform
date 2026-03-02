"""
Tests for pre-trade risk checks.

Unit tests cover RiskChecker directly (no API layer).
Integration tests verify the API returns 422 on violations.
"""

import pytest
from datetime import datetime, timezone
from decimal import Decimal

from trading.events.models import Order, OrderSide, OrderType, Trade
from trading.risk.checker import (
    MAX_POSITION_QUANTITY,
    RiskChecker,
    RiskViolation,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


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


# ── Position limit ────────────────────────────────────────────────────────────


def test_position_limit_allows_order_within_limit():
    checker = RiskChecker(max_position=1000)
    checker.check(make_order(quantity=500))  # should not raise


def test_position_limit_rejects_order_exceeding_limit():
    checker = RiskChecker(max_position=1000)
    with pytest.raises(RiskViolation, match="Position limit exceeded"):
        checker.check(make_order(quantity=1001))


def test_position_limit_accumulates_across_fills():
    checker = RiskChecker(max_position=1000)
    trade = make_trade(buyer_order_id="O-1", quantity=700)
    checker.record_fill(trade, buyer_account="acc1", seller_account="acc2")

    # acc1 now has 700 — another 400 would be 1100 > 1000
    with pytest.raises(RiskViolation, match="Position limit exceeded"):
        checker.check(make_order(quantity=400, account_id="acc1"))


def test_position_limit_allows_after_sell_reduces_position():
    checker = RiskChecker(max_position=1000)
    buy_trade = make_trade(buyer_order_id="O-1", quantity=800)
    checker.record_fill(buy_trade, buyer_account="acc1", seller_account="acc2")

    sell_trade = make_trade(buyer_order_id="O-3", seller_order_id="O-2", quantity=300)
    checker.record_fill(sell_trade, buyer_account="acc3", seller_account="acc1")

    # Position is now 500 — 400 more = 900 <= 1000
    checker.check(make_order(quantity=400, account_id="acc1"))  # should not raise


def test_position_limit_is_independent_per_ticker():
    checker = RiskChecker(max_position=1000)
    trade = make_trade(buyer_order_id="O-1", quantity=900, ticker="AAPL")
    checker.record_fill(trade, buyer_account="acc1", seller_account="acc2")

    # MSFT is a separate book — 200 shares is fine
    checker.check(make_order(quantity=200, ticker="MSFT", account_id="acc1"))


def test_position_limit_is_independent_per_account():
    checker = RiskChecker(max_position=1000)
    trade = make_trade(buyer_order_id="O-1", quantity=900, ticker="AAPL")
    checker.record_fill(trade, buyer_account="acc1", seller_account="acc2")

    # acc2 has no position — 1000 shares is fine
    checker.check(make_order(quantity=1000, account_id="acc2"))


# ── Notional exposure ─────────────────────────────────────────────────────────


def test_notional_exposure_allows_order_within_limit():
    checker = RiskChecker(max_notional=Decimal("100_000"))
    # 500 × $100 = $50,000 — within limit
    checker.check(make_order(quantity=500, price="100.00"))


def test_notional_exposure_rejects_order_exceeding_limit():
    checker = RiskChecker(max_notional=Decimal("100_000"))
    # 2000 × $100 = $200,000 — exceeds $100k
    with pytest.raises(RiskViolation, match="Notional exposure limit exceeded"):
        checker.check(make_order(quantity=2000, price="100.00"))


def test_notional_exposure_accumulates_across_open_orders():
    checker = RiskChecker(max_notional=Decimal("100_000"))
    order1 = make_order(quantity=600, price="100.00", order_id="O-1")
    checker.check(order1)
    checker.record_open_order(order1)

    # $60,000 open + $50,000 new = $110,000 > $100,000
    with pytest.raises(RiskViolation, match="Notional exposure limit exceeded"):
        checker.check(make_order(quantity=500, price="100.00", order_id="O-2"))


def test_notional_exposure_reduced_after_cancel():
    checker = RiskChecker(max_notional=Decimal("100_000"))
    order1 = make_order(quantity=600, price="100.00", order_id="O-1")
    checker.check(order1)
    checker.record_open_order(order1)

    checker.record_cancel(order1)  # exposure drops back to $0

    checker.check(make_order(quantity=700, price="100.00", order_id="O-2"))


def test_notional_exposure_not_checked_for_market_orders():
    checker = RiskChecker(max_notional=Decimal("100"))
    # Market order — notional check is skipped; quantity within position limit
    checker.check(make_order(quantity=1, price=None, order_type=OrderType.MARKET))


def test_get_position_returns_zero_for_unknown_account():
    assert RiskChecker().get_position("unknown", "AAPL") == 0


def test_get_notional_exposure_returns_decimal_zero_for_unknown_account():
    result = RiskChecker().get_notional_exposure("unknown")
    assert result == Decimal("0")
    assert isinstance(result, Decimal)


# ── Spread protection ─────────────────────────────────────────────────────────


def test_spread_check_passes_with_tight_spread():
    checker = RiskChecker(max_spread=Decimal("10.00"))
    checker.check_market_spread("AAPL", Decimal("99.00"), Decimal("100.00"))


def test_spread_check_rejects_wide_spread():
    checker = RiskChecker(max_spread=Decimal("10.00"))
    with pytest.raises(RiskViolation, match="spread"):
        checker.check_market_spread("AAPL", Decimal("50.00"), Decimal("150.00"))


def test_spread_check_rejects_empty_book():
    checker = RiskChecker()
    with pytest.raises(RiskViolation, match="empty"):
        checker.check_market_spread("AAPL", None, None)


def test_spread_check_allows_one_sided_book():
    checker = RiskChecker()
    # Ask side exists — execution may succeed; let the engine decide
    checker.check_market_spread("AAPL", None, Decimal("150.00"))


# ── API integration ───────────────────────────────────────────────────────────


async def test_api_rejects_order_exceeding_position_limit(client):
    response = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": MAX_POSITION_QUANTITY + 1,
            "price": "1.00",
        },
    )
    assert response.status_code == 422
    assert "Position limit exceeded" in response.json()["detail"]


async def test_api_rejects_order_exceeding_notional_limit(client):
    # 10,000 × $200 = $2,000,000 > $1,000,000 limit
    response = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 10_000,
            "price": "200.00",
        },
    )
    assert response.status_code == 422
    assert "Notional exposure limit exceeded" in response.json()["detail"]


async def test_api_rejects_market_order_on_empty_book(client):
    response = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": 10,
        },
    )
    assert response.status_code == 422
    assert "empty" in response.json()["detail"].lower()


async def test_api_cancel_reduces_notional_exposure(client):
    """Cancelling an order frees up notional exposure for future orders."""
    # Spread exposure across two tickers so neither per-ticker limit ($500k) is hit
    # while approaching the total limit ($1M).
    # AAPL: 4,500 × $100 = $450,000  (within $500k per-ticker)
    resp_aapl = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 4_500,
            "price": "100.00",
            "account_id": "acc-notional-cancel",
        },
    )
    assert resp_aapl.status_code == 201
    aapl_order_id = resp_aapl.json()["order_id"]

    # MSFT: 4,500 × $100 = $450,000 — total now $900,000 (within $1M)
    resp_msft = await client.post(
        "/orders",
        json={
            "ticker": "MSFT",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 4_500,
            "price": "100.00",
            "account_id": "acc-notional-cancel",
        },
    )
    assert resp_msft.status_code == 201

    # GOOGL: 2,000 × $100 = $200,000 — total $1,100,000 > $1,000,000 → rejected
    resp_bad = await client.post(
        "/orders",
        json={
            "ticker": "GOOGL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 2_000,
            "price": "100.00",
            "account_id": "acc-notional-cancel",
        },
    )
    assert resp_bad.status_code == 422
    assert "Notional exposure limit exceeded" in resp_bad.json()["detail"]

    # Cancel the AAPL order — total drops back to $450,000
    await client.post(f"/orders/{aapl_order_id}/cancel")

    # Now the GOOGL order passes: $450,000 + $200,000 = $650,000 < $1,000,000
    resp_good = await client.post(
        "/orders",
        json={
            "ticker": "GOOGL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 2_000,
            "price": "100.00",
            "account_id": "acc-notional-cancel",
        },
    )
    assert resp_good.status_code == 201


# ── Per-ticker notional exposure ──────────────────────────────────────────────


async def test_per_ticker_notional_rejected_when_limit_exceeded(client):
    """
    $400k open in AAPL + $200k new order = $600k > $500k per-ticker limit → 422.
    """
    resp1 = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 4_000,
            "price": "100.00",  # $400,000
            "account_id": "acc-ticker",
        },
    )
    assert resp1.status_code == 201

    resp2 = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 2_000,
            "price": "100.00",  # $200,000 → total $600,000 > $500,000
            "account_id": "acc-ticker",
        },
    )
    assert resp2.status_code == 422
    assert "Per-ticker notional limit exceeded" in resp2.json()["detail"]


async def test_per_ticker_notional_independent_across_tickers(client):
    """
    $400k in AAPL and $400k in MSFT are each within the $500k per-ticker limit.
    """
    resp1 = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 4_000,
            "price": "100.00",  # $400,000 in AAPL
            "account_id": "acc-two-tickers",
        },
    )
    assert resp1.status_code == 201

    resp2 = await client.post(
        "/orders",
        json={
            "ticker": "MSFT",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 4_000,
            "price": "100.00",  # $400,000 in MSFT — different ticker
            "account_id": "acc-two-tickers",
        },
    )
    assert resp2.status_code == 201


async def test_per_ticker_notional_freed_after_cancel(client):
    """
    Cancelling a $400k AAPL order frees the exposure so another $400k order passes.
    """
    resp1 = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 4_000,
            "price": "100.00",
            "account_id": "acc-cancel-ticker",
        },
    )
    assert resp1.status_code == 201
    order_id = resp1.json()["order_id"]

    await client.post(f"/orders/{order_id}/cancel")

    resp2 = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 4_000,
            "price": "100.00",
            "account_id": "acc-cancel-ticker",
        },
    )
    assert resp2.status_code == 201


async def test_per_ticker_notional_reduced_after_fill(client):
    """
    After a $200k BUY fills against a resting SELL, the buyer's per-ticker
    notional drops to $0 so a subsequent $400k order passes.
    """
    # Resting sell provides liquidity
    await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 2_000,
            "price": "100.00",
            "account_id": "acc-sell-fill",
        },
    )

    # Buy matches and fills — notional is reduced to $0 after fill
    resp_buy = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 2_000,
            "price": "100.00",  # $200,000 — fills immediately
            "account_id": "acc-buy-fill",
        },
    )
    assert resp_buy.status_code == 201
    assert resp_buy.json()["filled_quantity"] == 2_000

    # Buyer's notional is back to $0 — $400k order should pass
    resp2 = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 4_000,
            "price": "100.00",  # $400,000 < $500,000 limit
            "account_id": "acc-buy-fill",
        },
    )
    assert resp2.status_code == 201
