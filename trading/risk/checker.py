"""
Pre-trade risk checks.

Enforces two limits before an order reaches the matching engine:

1. Position limit: net filled quantity per (account, ticker) <= MAX_POSITION_QUANTITY
2. Notional exposure: total open LIMIT order value per account <= MAX_NOTIONAL_EXPOSURE

The checker is stateful: it tracks confirmed fills and open orders.
The consumer calls record_fill() and record_cancel() to keep state current.
"""

from dataclasses import dataclass
from decimal import Decimal

from trading.events.models import Order, OrderType, Trade

MAX_POSITION_QUANTITY: int = 10_000
MAX_NOTIONAL_EXPOSURE: Decimal = Decimal("1_000_000")
MAX_SPREAD: Decimal = Decimal("50.00")


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
        max_spread: Decimal = MAX_SPREAD,
    ) -> None:
        self._max_position = max_position
        self._max_notional = max_notional
        self._max_spread = max_spread
        # account_id -> ticker -> confirmed net filled quantity
        self._positions: dict[str, dict[str, int]] = {}
        # account_id -> {(order_id, quantity, price)} for open LIMIT orders
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
            # One-sided book — still allows execution against the available side.
            return
        spread = best_ask - best_bid
        if spread > self._max_spread:
            raise RiskViolation(
                f"Market order rejected for {ticker}: spread {spread} "
                f"exceeds limit {self._max_spread}"
            )

    def record_open_order(self, order: Order) -> None:
        """
        Register a LIMIT order as open (adds to notional exposure tracking).

        Called after check() passes and before submit_order().
        """
        if order.order_type != OrderType.LIMIT:
            return
        price = order.price if order.price is not None else Decimal("0")
        self._open_orders.setdefault(order.account_id, set()).add(
            (order.order_id, order.quantity, price)
        )

    def record_fill(
        self, trade: Trade, buyer_account: str, seller_account: str
    ) -> None:
        """
        Update position state after a confirmed trade.

        Called by the consumer once per trade, after engine.submit_order().
        """
        self._add_position(buyer_account, trade.ticker, trade.quantity)
        self._add_position(seller_account, trade.ticker, -trade.quantity)

    def record_cancel(self, order: Order) -> None:
        """
        Remove a cancelled LIMIT order from open exposure tracking.

        Called after a successful cancellation.
        """
        if order.order_type != OrderType.LIMIT:
            return
        price = order.price if order.price is not None else Decimal("0")
        self._open_orders.get(order.account_id, set()).discard(
            (order.order_id, order.quantity, price)
        )

    def record_order_complete(self, order: Order) -> None:
        """
        Remove a fully filled order from open exposure tracking.

        Called by the consumer after all trades for an order are recorded.
        """
        self.record_cancel(order)

    def get_position(self, account_id: str, ticker: str) -> int:
        """Return the current net filled position for an (account, ticker) pair."""
        return self._positions.get(account_id, {}).get(ticker, 0)

    def get_notional_exposure(self, account_id: str) -> Decimal:
        """Return the total notional value of open LIMIT orders for an account."""
        return sum(
            (qty * price for _, qty, price in self._open_orders.get(account_id, set())),
            Decimal("0"),
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
