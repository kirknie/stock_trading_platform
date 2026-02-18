"""
Single-ticker order book with price-time priority matching.

This module implements a limit order book for a single trading instrument.
Orders are matched using price-time priority:
- Buy orders: highest price first, then FIFO
- Sell orders: lowest price first, then FIFO

Key features:
- Deterministic matching
- Partial fill support
- Market and limit orders
- O(1) best bid/ask access
"""

from decimal import Decimal
from collections import defaultdict, deque
from typing import Dict, Deque, List, Optional
from trading.events.models import Order, OrderSide, Trade, OrderStatus, OrderType
from datetime import datetime


class OrderBook:
    """Single-ticker order book with price-time priority."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        # Buy side: higher prices first (use negative for sorting)
        self.bids: Dict[Decimal, Deque[Order]] = defaultdict(deque)
        # Sell side: lower prices first
        self.asks: Dict[Decimal, Deque[Order]] = defaultdict(deque)
        self._trade_counter = 0

    def add_limit_order(self, order: Order) -> List[Trade]:
        """Add limit order and return any trades generated."""
        assert order.order_type == OrderType.LIMIT
        assert order.price is not None
        assert order.ticker == self.ticker

        trades = []

        if order.side == OrderSide.BUY:
            trades = self._match_buy_order(order)
        else:
            trades = self._match_sell_order(order)

        # If order not fully filled, add to book
        if not order.is_complete() and order.remaining_quantity() > 0:
            if order.side == OrderSide.BUY:
                self.bids[order.price].append(order)
            else:
                self.asks[order.price].append(order)

        return trades

    def execute_market_order(self, order: Order) -> List[Trade]:
        """Execute market order immediately or reject."""
        assert order.order_type == OrderType.MARKET
        assert order.ticker == self.ticker

        trades = []

        if order.side == OrderSide.BUY:
            trades = self._match_buy_order(order)
        else:
            trades = self._match_sell_order(order)

        # Market orders never rest in book
        if order.remaining_quantity() > 0:
            order.status = OrderStatus.REJECTED

        return trades

    def _match_buy_order(self, buy_order: Order) -> List[Trade]:
        """Match a buy order against asks."""
        trades = []

        while buy_order.remaining_quantity() > 0 and self.asks:
            best_ask_price = min(self.asks.keys())

            # Price check for limit orders
            if buy_order.order_type == OrderType.LIMIT:
                if buy_order.price < best_ask_price:
                    break

            # Match against best ask
            ask_queue = self.asks[best_ask_price]

            while buy_order.remaining_quantity() > 0 and ask_queue:
                sell_order = ask_queue[0]

                # Calculate trade quantity
                trade_qty = min(
                    buy_order.remaining_quantity(),
                    sell_order.remaining_quantity()
                )

                # Create trade
                trade = Trade(
                    trade_id=f"T{self._trade_counter}",
                    ticker=self.ticker,
                    buyer_order_id=buy_order.order_id,
                    seller_order_id=sell_order.order_id,
                    price=best_ask_price,  # Trade at resting order price
                    quantity=trade_qty,
                    timestamp=datetime.now()
                )
                trades.append(trade)
                self._trade_counter += 1

                # Update orders
                buy_order.filled_quantity += trade_qty
                sell_order.filled_quantity += trade_qty

                # Update statuses
                if buy_order.remaining_quantity() == 0:
                    buy_order.status = OrderStatus.FILLED
                elif buy_order.filled_quantity > 0:
                    buy_order.status = OrderStatus.PARTIALLY_FILLED

                if sell_order.remaining_quantity() == 0:
                    sell_order.status = OrderStatus.FILLED
                    ask_queue.popleft()
                elif sell_order.filled_quantity > 0:
                    sell_order.status = OrderStatus.PARTIALLY_FILLED

            # Clean up empty price level
            if not ask_queue:
                del self.asks[best_ask_price]

        return trades

    def _match_sell_order(self, sell_order: Order) -> List[Trade]:
        """Match a sell order against bids."""
        trades = []

        while sell_order.remaining_quantity() > 0 and self.bids:
            best_bid_price = max(self.bids.keys())

            # Price check for limit orders
            if sell_order.order_type == OrderType.LIMIT:
                if sell_order.price > best_bid_price:
                    break

            # Match against best bid
            bid_queue = self.bids[best_bid_price]

            while sell_order.remaining_quantity() > 0 and bid_queue:
                buy_order = bid_queue[0]

                # Calculate trade quantity
                trade_qty = min(
                    sell_order.remaining_quantity(),
                    buy_order.remaining_quantity()
                )

                # Create trade
                trade = Trade(
                    trade_id=f"T{self._trade_counter}",
                    ticker=self.ticker,
                    buyer_order_id=buy_order.order_id,
                    seller_order_id=sell_order.order_id,
                    price=best_bid_price,  # Trade at resting order price
                    quantity=trade_qty,
                    timestamp=datetime.now()
                )
                trades.append(trade)
                self._trade_counter += 1

                # Update orders
                sell_order.filled_quantity += trade_qty
                buy_order.filled_quantity += trade_qty

                # Update statuses
                if sell_order.remaining_quantity() == 0:
                    sell_order.status = OrderStatus.FILLED
                elif sell_order.filled_quantity > 0:
                    sell_order.status = OrderStatus.PARTIALLY_FILLED

                if buy_order.remaining_quantity() == 0:
                    buy_order.status = OrderStatus.FILLED
                    bid_queue.popleft()
                elif buy_order.filled_quantity > 0:
                    buy_order.status = OrderStatus.PARTIALLY_FILLED

            # Clean up empty price level
            if not bid_queue:
                del self.bids[best_bid_price]

        return trades

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID. Returns True if found and canceled."""
        # Search bids
        for price, queue in list(self.bids.items()):
            for order in queue:
                if order.order_id == order_id:
                    order.status = OrderStatus.CANCELED
                    queue.remove(order)
                    if not queue:
                        del self.bids[price]
                    return True

        # Search asks
        for price, queue in list(self.asks.items()):
            for order in queue:
                if order.order_id == order_id:
                    order.status = OrderStatus.CANCELED
                    queue.remove(order)
                    if not queue:
                        del self.asks[price]
                    return True

        return False

    def get_best_bid(self) -> Optional[Decimal]:
        """Get best bid price."""
        return max(self.bids.keys()) if self.bids else None

    def get_best_ask(self) -> Optional[Decimal]:
        """Get best ask price."""
        return min(self.asks.keys()) if self.asks else None

    def get_spread(self) -> Optional[Decimal]:
        """Get bid-ask spread."""
        best_bid = self.get_best_bid()
        best_ask = self.get_best_ask()
        if best_bid and best_ask:
            return best_ask - best_bid
        return None
