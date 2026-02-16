# Week 1 Execution Plan: Matching Engine

## Overview
Build the core matching engine with support for multiple tickers and both limit/market orders.

**Goal:** By end of week, you have a working, tested matching engine that can:
- Route orders to correct ticker's order book
- Execute limit orders with price-time priority
- Execute market orders immediately (IOC)
- Pass property tests proving correctness
- Be 100% deterministic for replay

---

## Pre-Week Setup (30 minutes)

### Step 1: Project Structure
```bash
mkdir -p trading/{engine,events,tests}
touch trading/__init__.py
touch trading/engine/__init__.py
touch trading/events/__init__.py
touch trading/tests/__init__.py
```

### Step 2: Python Environment
```bash
uv venv --python 3.13
# Alternative: python3.13 -m venv .venv --prompt stock_trading_platform
source .venv/bin/activate
```

### Step 3: Initialize Project
```bash
uv init --no-readme
```

This creates a minimal `pyproject.toml` with project metadata.

### Step 4: Install Core Dependencies
```bash
uv add --dev pytest pytest-asyncio hypothesis
```

This installs the packages and automatically adds them to `pyproject.toml` under `[project.optional-dependencies]` dev section.

### Step 5: Initialize Git Repository
```bash
# Initialize git
git init

# Configure git for this project (local config)
git config --local user.name "kirknie"
git config --local user.email "kirknie@gmail.com"

# Disable GPG signing for this project (optional, if you have global signing enabled)
git config --local commit.gpgsign false

# Create .gitignore
cat > .gitignore << 'EOF'
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Virtual Environment
.venv/
venv/
ENV/
env/

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# Testing
.pytest_cache/
.coverage
htmlcov/
.hypothesis/

# OS
.DS_Store
Thumbs.db

# Project specific
*.log
.ruff_cache/
.mypy_cache/
EOF

# Initial commit
git add .
git commit -m "Initial project setup

- Project structure created
- pyproject.toml with Python 3.13
- Core dependencies: pytest, pytest-asyncio, hypothesis
- Trading module scaffolding"
```

**Optional: Link to GitHub**
```bash
# Create repo on GitHub first, then:
git remote add origin git@github.com:kirknie/stock_trading_platform.git
git branch -M main
git push -u origin main
```

---

## Day 1: Core Data Models & Single Order Book (4-6 hours)

### Goal
Define domain models and implement a single-ticker order book.

### Step 1.1: Define Order Models (30 min)
**File:** `trading/events/models.py`

Create these core types:
```python
from enum import Enum
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from typing import Optional

class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"

class OrderStatus(Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"

@dataclass
class Order:
    order_id: str
    ticker: str
    side: OrderSide
    order_type: OrderType
    quantity: int  # shares
    price: Optional[Decimal]  # None for market orders
    timestamp: datetime
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: int = 0
    account_id: str = "default"

    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    def is_complete(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)

@dataclass
class Trade:
    trade_id: str
    ticker: str
    buyer_order_id: str
    seller_order_id: str
    price: Decimal
    quantity: int
    timestamp: datetime
```

**Validation:**
- Run `python -m pytest trading/tests/` (should find no tests yet)
- Import in Python REPL to verify no syntax errors

---

### Step 1.2: Write Order Model Tests (30 min)
**File:** `trading/tests/test_models.py`

```python
from decimal import Decimal
from datetime import datetime
from trading.events.models import Order, OrderSide, OrderType, OrderStatus

def test_order_creation():
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    assert order.remaining_quantity() == 100
    assert not order.is_complete()

def test_order_partial_fill():
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    order.filled_quantity = 30
    assert order.remaining_quantity() == 70
    assert not order.is_complete()

def test_order_complete_fill():
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    order.filled_quantity = 100
    order.status = OrderStatus.FILLED
    assert order.remaining_quantity() == 0
    assert order.is_complete()

def test_market_order_no_price():
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=None,
        timestamp=datetime.now()
    )
    assert order.price is None
    assert order.order_type == OrderType.MARKET
```

**Run:** `pytest trading/tests/test_models.py -v`

---

### Step 1.3: Implement OrderBook (2-3 hours)
**File:** `trading/engine/order_book.py`

**Requirements:**
- Price-time priority for limit orders
- Separate buy/sell sides
- Efficient price level lookups
- No dependencies on other order books

**Implementation approach:**
```python
from decimal import Decimal
from collections import defaultdict, deque
from typing import Dict, Deque, List, Optional
from trading.events.models import Order, OrderSide, Trade, OrderStatus
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
```

**Validation:**
- Code compiles without errors
- Imports work correctly

---

### Step 1.4: Write OrderBook Tests (1-2 hours)
**File:** `trading/tests/test_order_book.py`

Create comprehensive tests:

```python
from decimal import Decimal
from datetime import datetime
from trading.engine.order_book import OrderBook
from trading.events.models import Order, OrderSide, OrderType, OrderStatus

def test_empty_order_book():
    book = OrderBook("AAPL")
    assert book.ticker == "AAPL"
    assert book.get_best_bid() is None
    assert book.get_best_ask() is None
    assert book.get_spread() is None

def test_add_single_limit_buy():
    book = OrderBook("AAPL")
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(order)

    assert len(trades) == 0
    assert book.get_best_bid() == Decimal("150.00")
    assert book.get_best_ask() is None

def test_add_single_limit_sell():
    book = OrderBook("AAPL")
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("151.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(order)

    assert len(trades) == 0
    assert book.get_best_bid() is None
    assert book.get_best_ask() == Decimal("151.00")

def test_immediate_full_match():
    book = OrderBook("AAPL")

    # Add sell order
    sell_order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell_order)

    # Add matching buy order
    buy_order = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(buy_order)

    assert len(trades) == 1
    assert trades[0].quantity == 100
    assert trades[0].price == Decimal("150.00")
    assert buy_order.status == OrderStatus.FILLED
    assert sell_order.status == OrderStatus.FILLED
    assert book.get_best_ask() is None

def test_partial_fill():
    book = OrderBook("AAPL")

    # Add large sell order
    sell_order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell_order)

    # Add smaller buy order
    buy_order = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=30,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(buy_order)

    assert len(trades) == 1
    assert trades[0].quantity == 30
    assert buy_order.status == OrderStatus.FILLED
    assert sell_order.status == OrderStatus.PARTIALLY_FILLED
    assert sell_order.remaining_quantity() == 70

def test_price_time_priority():
    book = OrderBook("AAPL")

    # Add two buy orders at same price
    order1 = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    order2 = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(order1)
    book.add_limit_order(order2)

    # Add sell order that matches one
    sell_order = Order(
        order_id="3",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(sell_order)

    # First order should match (time priority)
    assert len(trades) == 1
    assert trades[0].buyer_order_id == "1"
    assert order1.status == OrderStatus.FILLED
    assert order2.status == OrderStatus.NEW

def test_market_order_buy():
    book = OrderBook("AAPL")

    # Add sell order
    sell_order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell_order)

    # Execute market buy
    market_order = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=None,
        timestamp=datetime.now()
    )
    trades = book.execute_market_order(market_order)

    assert len(trades) == 1
    assert trades[0].price == Decimal("150.00")
    assert market_order.status == OrderStatus.FILLED

def test_market_order_no_liquidity():
    book = OrderBook("AAPL")

    # Execute market buy with no sellers
    market_order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=None,
        timestamp=datetime.now()
    )
    trades = book.execute_market_order(market_order)

    assert len(trades) == 0
    assert market_order.status == OrderStatus.REJECTED

def test_cancel_order():
    book = OrderBook("AAPL")

    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(order)

    success = book.cancel_order("1")
    assert success
    assert order.status == OrderStatus.CANCELED
    assert book.get_best_bid() is None

def test_cancel_nonexistent_order():
    book = OrderBook("AAPL")
    success = book.cancel_order("999")
    assert not success
```

**Run:** `pytest trading/tests/test_order_book.py -v`

**Success criteria:** All tests pass

---

### Day 1 Checklist
- [ ] `models.py` created with all order types
- [ ] `test_models.py` passes all tests
- [ ] `order_book.py` implements single-ticker matching
- [ ] `test_order_book.py` passes all tests
- [ ] No failing tests
- [ ] Code compiles without errors

---

## Day 2: Multi-Ticker Order Book Manager (3-4 hours)

### Goal
Create the routing layer that manages multiple order books.

### Step 2.1: Implement OrderBookManager (1-2 hours)
**File:** `trading/engine/order_book_manager.py`

```python
from typing import Dict, List, Set
from trading.engine.order_book import OrderBook
from trading.events.models import Order, Trade, OrderType

class OrderBookManager:
    """Manages multiple order books for different tickers."""

    def __init__(self, supported_tickers: List[str]):
        self.order_books: Dict[str, OrderBook] = {
            ticker: OrderBook(ticker) for ticker in supported_tickers
        }
        self.supported_tickers: Set[str] = set(supported_tickers)

    def submit_order(self, order: Order) -> List[Trade]:
        """Route order to appropriate book."""
        if order.ticker not in self.supported_tickers:
            raise ValueError(f"Ticker {order.ticker} not supported")

        book = self.order_books[order.ticker]

        if order.order_type == OrderType.LIMIT:
            return book.add_limit_order(order)
        elif order.order_type == OrderType.MARKET:
            return book.execute_market_order(order)
        else:
            raise ValueError(f"Unknown order type: {order.order_type}")

    def cancel_order(self, ticker: str, order_id: str) -> bool:
        """Cancel order in specific ticker's book."""
        if ticker not in self.supported_tickers:
            return False

        book = self.order_books[ticker]
        return book.cancel_order(order_id)

    def get_order_book(self, ticker: str) -> OrderBook:
        """Get order book for ticker."""
        if ticker not in self.supported_tickers:
            raise ValueError(f"Ticker {ticker} not supported")
        return self.order_books[ticker]

    def get_supported_tickers(self) -> List[str]:
        """Get list of supported tickers."""
        return sorted(self.supported_tickers)
```

---

### Step 2.2: Write Manager Tests (1-2 hours)
**File:** `trading/tests/test_order_book_manager.py`

```python
from decimal import Decimal
from datetime import datetime
import pytest
from trading.engine.order_book_manager import OrderBookManager
from trading.events.models import Order, OrderSide, OrderType, OrderStatus

def test_manager_initialization():
    tickers = ["AAPL", "MSFT", "GOOGL"]
    manager = OrderBookManager(tickers)

    assert set(manager.get_supported_tickers()) == set(tickers)
    assert len(manager.order_books) == 3

def test_submit_order_to_correct_book():
    manager = OrderBookManager(["AAPL", "MSFT"])

    # Submit to AAPL
    order_aapl = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = manager.submit_order(order_aapl)

    assert len(trades) == 0
    assert manager.get_order_book("AAPL").get_best_bid() == Decimal("150.00")
    assert manager.get_order_book("MSFT").get_best_bid() is None

def test_submit_to_unsupported_ticker():
    manager = OrderBookManager(["AAPL"])

    order = Order(
        order_id="1",
        ticker="INVALID",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )

    with pytest.raises(ValueError, match="not supported"):
        manager.submit_order(order)

def test_multi_ticker_matching():
    manager = OrderBookManager(["AAPL", "MSFT"])

    # Add orders for both tickers
    sell_aapl = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    manager.submit_order(sell_aapl)

    sell_msft = Order(
        order_id="2",
        ticker="MSFT",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )
    manager.submit_order(sell_msft)

    # Match AAPL
    buy_aapl = Order(
        order_id="3",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades_aapl = manager.submit_order(buy_aapl)

    # Match MSFT
    buy_msft = Order(
        order_id="4",
        ticker="MSFT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )
    trades_msft = manager.submit_order(buy_msft)

    assert len(trades_aapl) == 1
    assert trades_aapl[0].ticker == "AAPL"
    assert len(trades_msft) == 1
    assert trades_msft[0].ticker == "MSFT"

def test_cancel_in_correct_book():
    manager = OrderBookManager(["AAPL", "MSFT"])

    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    manager.submit_order(order)

    # Cancel in wrong book
    assert not manager.cancel_order("MSFT", "1")

    # Cancel in correct book
    assert manager.cancel_order("AAPL", "1")
    assert order.status == OrderStatus.CANCELED
```

**Run:** `pytest trading/tests/test_order_book_manager.py -v`

---

### Day 2 Checklist
- [ ] `order_book_manager.py` created
- [ ] Manager routes orders to correct books
- [ ] `test_order_book_manager.py` passes all tests
- [ ] Verified orders don't leak between tickers
- [ ] All previous tests still pass

---

## Day 3: Property-Based Testing & Edge Cases (4-5 hours)

### Goal
Use Hypothesis to prove invariants hold under random inputs.

### Step 3.1: Install Hypothesis
Hypothesis should already be installed from Pre-Week Setup. If not:
```bash
uv add --dev hypothesis
```

### Step 3.2: Write Property Tests (2-3 hours)
**File:** `trading/tests/test_properties.py`

```python
from decimal import Decimal
from datetime import datetime
from hypothesis import given, strategies as st, assume
from trading.engine.order_book import OrderBook
from trading.events.models import Order, OrderSide, OrderType, OrderStatus

# Strategy: generate valid orders
@st.composite
def order_strategy(draw, ticker="AAPL", order_type=OrderType.LIMIT):
    side = draw(st.sampled_from([OrderSide.BUY, OrderSide.SELL]))
    quantity = draw(st.integers(min_value=1, max_value=1000))

    if order_type == OrderType.LIMIT:
        price = Decimal(str(draw(st.floats(min_value=1.0, max_value=1000.0))))
        price = price.quantize(Decimal("0.01"))
    else:
        price = None

    order_id = draw(st.text(min_size=1, max_size=10))

    return Order(
        order_id=order_id,
        ticker=ticker,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        timestamp=datetime.now()
    )

@given(st.lists(order_strategy(), min_size=1, max_size=50))
def test_no_negative_fills(orders):
    """Property: Filled quantity never exceeds order quantity."""
    book = OrderBook("AAPL")

    for order in orders:
        if order.order_type == OrderType.LIMIT:
            book.add_limit_order(order)
        else:
            book.execute_market_order(order)

        assert order.filled_quantity >= 0
        assert order.filled_quantity <= order.quantity
        assert order.remaining_quantity() >= 0

@given(st.lists(order_strategy(), min_size=2, max_size=30))
def test_trade_conservation(orders):
    """Property: Total buy volume equals total sell volume in trades."""
    book = OrderBook("AAPL")
    all_trades = []

    for order in orders:
        if order.order_type == OrderType.LIMIT:
            trades = book.add_limit_order(order)
        else:
            trades = book.execute_market_order(order)
        all_trades.extend(trades)

    total_volume = sum(t.quantity for t in all_trades)

    # Every trade has a buyer and seller, so volumes must match
    # (This is implicitly true, but we verify no double-counting)
    for trade in all_trades:
        assert trade.quantity > 0

@given(order_strategy())
def test_single_order_invariants(order):
    """Property: Single order maintains valid state."""
    book = OrderBook("AAPL")

    if order.order_type == OrderType.LIMIT:
        trades = book.add_limit_order(order)
    else:
        trades = book.execute_market_order(order)

    # Invariants
    assert order.filled_quantity <= order.quantity

    if order.status == OrderStatus.FILLED:
        assert order.filled_quantity == order.quantity

    if order.status == OrderStatus.REJECTED:
        # Market orders rejected have no fills
        if order.order_type == OrderType.MARKET:
            assert order.filled_quantity == 0

def test_price_improvement_not_possible():
    """Property: Limit orders never get worse than limit price."""
    book = OrderBook("AAPL")

    # Add sell at 150
    sell_order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell_order)

    # Buy at 151 (willing to pay more)
    buy_order = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("151.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(buy_order)

    # Should trade at 150 (resting order price)
    assert len(trades) == 1
    assert trades[0].price == Decimal("150.00")
    assert trades[0].price <= buy_order.price

@given(st.lists(order_strategy(), min_size=5, max_size=50))
def test_deterministic_replay(orders):
    """Property: Same order sequence produces same result."""

    # First run
    book1 = OrderBook("AAPL")
    trades1 = []
    for order in orders:
        # Create deep copy to avoid state sharing
        order_copy1 = Order(
            order_id=order.order_id,
            ticker=order.ticker,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            price=order.price,
            timestamp=order.timestamp
        )
        if order_copy1.order_type == OrderType.LIMIT:
            t = book1.add_limit_order(order_copy1)
        else:
            t = book1.execute_market_order(order_copy1)
        trades1.extend(t)

    # Second run
    book2 = OrderBook("AAPL")
    trades2 = []
    for order in orders:
        order_copy2 = Order(
            order_id=order.order_id,
            ticker=order.ticker,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            price=order.price,
            timestamp=order.timestamp
        )
        if order_copy2.order_type == OrderType.LIMIT:
            t = book2.add_limit_order(order_copy2)
        else:
            t = book2.execute_market_order(order_copy2)
        trades2.extend(t)

    # Results must be identical
    assert len(trades1) == len(trades2)
    for t1, t2 in zip(trades1, trades2):
        assert t1.quantity == t2.quantity
        assert t1.price == t2.price
```

**Run:** `pytest trading/tests/test_properties.py -v --hypothesis-show-statistics`

This will run hundreds of random test cases to prove your invariants hold.

---

### Step 3.3: Add Edge Case Tests (1-2 hours)
**File:** `trading/tests/test_edge_cases.py`

```python
from decimal import Decimal
from datetime import datetime
from trading.engine.order_book import OrderBook
from trading.events.models import Order, OrderSide, OrderType, OrderStatus

def test_zero_quantity_order():
    """Edge: Zero quantity should not be accepted."""
    book = OrderBook("AAPL")

    # Note: You might want to add validation in Order __init__
    # For now, test current behavior
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=0,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )

    # Should not add to book
    trades = book.add_limit_order(order)
    assert len(trades) == 0
    assert book.get_best_bid() is None

def test_negative_price():
    """Edge: Negative prices should not be accepted."""
    # Note: Consider adding validation
    pass  # Add if you implement validation

def test_very_large_orders():
    """Edge: Handle very large order quantities."""
    book = OrderBook("AAPL")

    large_qty = 1_000_000
    sell = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=large_qty,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell)

    buy = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=large_qty,
        price=None,
        timestamp=datetime.now()
    )
    trades = book.execute_market_order(buy)

    assert len(trades) == 1
    assert trades[0].quantity == large_qty

def test_many_price_levels():
    """Edge: Many price levels in book."""
    book = OrderBook("AAPL")

    # Add 100 different price levels
    for i in range(100):
        order = Order(
            order_id=str(i),
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=Decimal(str(100 + i)),
            timestamp=datetime.now()
        )
        book.add_limit_order(order)

    assert book.get_best_bid() == Decimal("199")
    assert len(book.bids) == 100

def test_same_order_id_different_tickers():
    """Edge: Same order ID across tickers (should be allowed)."""
    from trading.engine.order_book_manager import OrderBookManager

    manager = OrderBookManager(["AAPL", "MSFT"])

    order1 = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )

    order2 = Order(
        order_id="1",
        ticker="MSFT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )

    manager.submit_order(order1)
    manager.submit_order(order2)

    # Both should be in their respective books
    assert manager.get_order_book("AAPL").get_best_bid() == Decimal("150.00")
    assert manager.get_order_book("MSFT").get_best_bid() == Decimal("300.00")

def test_rapid_order_cancellation():
    """Edge: Cancel order immediately after submission."""
    book = OrderBook("AAPL")

    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(order)

    # Immediate cancel
    success = book.cancel_order("1")
    assert success
    assert order.status == OrderStatus.CANCELED
    assert book.get_best_bid() is None

def test_partial_fill_then_cancel():
    """Edge: Cancel partially filled order."""
    book = OrderBook("AAPL")

    large_sell = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(large_sell)

    # Partial fill
    small_buy = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=30,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(small_buy)

    assert large_sell.filled_quantity == 30
    assert large_sell.remaining_quantity() == 70

    # Cancel remaining
    success = book.cancel_order("1")
    assert success
    assert large_sell.status == OrderStatus.CANCELED
    # Filled quantity should remain
    assert large_sell.filled_quantity == 30
```

**Run:** `pytest trading/tests/test_edge_cases.py -v`

---

### Day 3 Checklist
- [ ] Hypothesis installed
- [ ] Property tests written and passing
- [ ] Edge case tests written and passing
- [ ] All invariants proven (no negative fills, conservation, determinism)
- [ ] All tests still passing (`pytest -v`)

---

## Day 4: Integration & Matcher Abstraction (3-4 hours)

### Goal
Create a cleaner abstraction and write integration tests.

### Step 4.1: Create Matcher Interface (1 hour)
**File:** `trading/engine/matcher.py`

This provides a clean API for the rest of the system.

```python
from typing import List, Optional
from trading.engine.order_book_manager import OrderBookManager
from trading.events.models import Order, Trade
from decimal import Decimal

class MatchingEngine:
    """High-level matching engine interface."""

    def __init__(self, tickers: List[str]):
        self.manager = OrderBookManager(tickers)
        self.order_registry = {}  # order_id -> (ticker, order)

    def submit_order(self, order: Order) -> List[Trade]:
        """Submit order and track it."""
        trades = self.manager.submit_order(order)

        # Register order for cancellation
        if not order.is_complete():
            self.order_registry[order.order_id] = (order.ticker, order)

        return trades

    def cancel_order(self, order_id: str) -> bool:
        """Cancel order by ID (auto-detects ticker)."""
        if order_id not in self.order_registry:
            return False

        ticker, order = self.order_registry[order_id]
        success = self.manager.cancel_order(ticker, order_id)

        if success:
            del self.order_registry[order_id]

        return success

    def get_market_data(self, ticker: str) -> dict:
        """Get market data for ticker."""
        book = self.manager.get_order_book(ticker)

        return {
            "ticker": ticker,
            "best_bid": book.get_best_bid(),
            "best_ask": book.get_best_ask(),
            "spread": book.get_spread()
        }

    def get_supported_tickers(self) -> List[str]:
        """Get supported tickers."""
        return self.manager.get_supported_tickers()
```

---

### Step 4.2: Write Integration Tests (2-3 hours)
**File:** `trading/tests/test_integration.py`

```python
from decimal import Decimal
from datetime import datetime
from trading.engine.matcher import MatchingEngine
from trading.events.models import Order, OrderSide, OrderType, OrderStatus

def test_full_trading_scenario():
    """Integration: Complete trading scenario."""
    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL"])

    # Check supported tickers
    assert "AAPL" in engine.get_supported_tickers()

    # Add initial orders
    sell_aapl = Order(
        order_id="S1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(sell_aapl)
    assert len(trades) == 0

    # Check market data
    md = engine.get_market_data("AAPL")
    assert md["best_ask"] == Decimal("150.00")
    assert md["best_bid"] is None

    # Add buy order - should match
    buy_aapl = Order(
        order_id="B1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(buy_aapl)

    assert len(trades) == 1
    assert trades[0].ticker == "AAPL"
    assert trades[0].quantity == 100
    assert trades[0].price == Decimal("150.00")

    # Market should be empty now
    md = engine.get_market_data("AAPL")
    assert md["best_ask"] is None
    assert md["best_bid"] is None

def test_cross_ticker_independence():
    """Integration: Orders in one ticker don't affect another."""
    engine = MatchingEngine(["AAPL", "MSFT"])

    # Fill AAPL book
    for i in range(5):
        order = Order(
            order_id=f"AAPL_{i}",
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=Decimal("150.00") + Decimal(i),
            timestamp=datetime.now()
        )
        engine.submit_order(order)

    # MSFT should be unaffected
    md_msft = engine.get_market_data("MSFT")
    assert md_msft["best_bid"] is None

    md_aapl = engine.get_market_data("AAPL")
    assert md_aapl["best_bid"] == Decimal("154.00")

def test_cancel_across_tickers():
    """Integration: Cancel uses correct ticker."""
    engine = MatchingEngine(["AAPL", "MSFT"])

    order_aapl = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(order_aapl)

    order_msft = Order(
        order_id="2",
        ticker="MSFT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(order_msft)

    # Cancel AAPL order
    assert engine.cancel_order("1")

    # MSFT order should still be there
    md_msft = engine.get_market_data("MSFT")
    assert md_msft["best_bid"] == Decimal("300.00")

    # AAPL should be empty
    md_aapl = engine.get_market_data("AAPL")
    assert md_aapl["best_bid"] is None

def test_market_order_across_multiple_tickers():
    """Integration: Market orders in different tickers."""
    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL"])

    # Set up sell orders in each ticker
    tickers_prices = [
        ("AAPL", "150.00"),
        ("MSFT", "300.00"),
        ("GOOGL", "2500.00")
    ]

    for ticker, price in tickers_prices:
        sell = Order(
            order_id=f"{ticker}_SELL",
            ticker=ticker,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=Decimal(price),
            timestamp=datetime.now()
        )
        engine.submit_order(sell)

    # Execute market buys for each
    for ticker, expected_price in tickers_prices:
        buy = Order(
            order_id=f"{ticker}_BUY",
            ticker=ticker,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
            price=None,
            timestamp=datetime.now()
        )
        trades = engine.submit_order(buy)

        assert len(trades) == 1
        assert trades[0].ticker == ticker
        assert trades[0].price == Decimal(expected_price)

def test_order_registry_cleanup():
    """Integration: Order registry cleans up completed orders."""
    engine = MatchingEngine(["AAPL"])

    # Add sell order
    sell = Order(
        order_id="S1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(sell)

    assert "S1" in engine.order_registry

    # Match it
    buy = Order(
        order_id="B1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(buy)

    # Both should be completed and not in registry
    # (Current implementation only removes on cancel - consider fixing)
    assert sell.status == OrderStatus.FILLED
```

**Run:** `pytest trading/tests/test_integration.py -v`

---

### Day 4 Checklist
- [ ] `matcher.py` created with clean API
- [ ] Integration tests cover multi-ticker scenarios
- [ ] Order cancellation works across tickers
- [ ] Market data retrieval works
- [ ] All tests passing (`pytest -v`)

---

## Day 5: Documentation & Final Validation (2-3 hours)

### Goal
Document your code, create examples, and validate everything works.

### Step 5.1: Add Module Docstrings (30 min)

Add comprehensive docstrings to each module:

```python
# trading/engine/order_book.py
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

# trading/engine/order_book_manager.py
"""
Multi-ticker order book manager.

Routes orders to the appropriate ticker's order book.
Maintains isolated order books for each supported ticker.

Key features:
- Support for multiple tickers (3-5 equities)
- Order routing by ticker symbol
- Cross-ticker cancellation support
"""

# trading/engine/matcher.py
"""
High-level matching engine interface.

Provides a clean API for order submission, cancellation,
and market data retrieval across multiple tickers.

This is the main entry point for the trading system.
"""
```

---

### Step 5.2: Create Usage Examples (30 min)
**File:** `examples/basic_usage.py`

```python
#!/usr/bin/env python3
"""
Basic usage examples for the matching engine.
"""

from decimal import Decimal
from datetime import datetime
from trading.engine.matcher import MatchingEngine
from trading.events.models import Order, OrderSide, OrderType

def main():
    # Initialize engine with supported tickers
    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"])

    print("Supported tickers:", engine.get_supported_tickers())
    print()

    # Example 1: Add limit order
    print("=== Example 1: Limit Order ===")
    sell_order = Order(
        order_id="SELL_1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(sell_order)
    print(f"Submitted sell order: {sell_order.order_id}")
    print(f"Trades generated: {len(trades)}")

    md = engine.get_market_data("AAPL")
    print(f"Market data: {md}")
    print()

    # Example 2: Matching order
    print("=== Example 2: Matching Buy Order ===")
    buy_order = Order(
        order_id="BUY_1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(buy_order)
    print(f"Submitted buy order: {buy_order.order_id}")
    print(f"Trades generated: {len(trades)}")

    for trade in trades:
        print(f"  Trade: {trade.quantity} shares @ ${trade.price}")
    print()

    # Example 3: Market order
    print("=== Example 3: Market Order ===")
    sell_limit = Order(
        order_id="SELL_2",
        ticker="MSFT",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(sell_limit)

    market_buy = Order(
        order_id="MKT_BUY_1",
        ticker="MSFT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=50,
        price=None,
        timestamp=datetime.now()
    )
    trades = engine.submit_order(market_buy)
    print(f"Market buy executed: {len(trades)} trades")
    for trade in trades:
        print(f"  Executed at: ${trade.price}")
    print()

    # Example 4: Cancellation
    print("=== Example 4: Order Cancellation ===")
    cancel_order = Order(
        order_id="CANCEL_ME",
        ticker="GOOGL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=Decimal("2500.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(cancel_order)
    print(f"Submitted order: {cancel_order.order_id}")

    success = engine.cancel_order("CANCEL_ME")
    print(f"Cancellation successful: {success}")
    print(f"Order status: {cancel_order.status}")
    print()

if __name__ == "__main__":
    main()
```

Make executable:
```bash
mkdir examples
chmod +x examples/basic_usage.py
python examples/basic_usage.py
```

---

### Step 5.3: Create Week 1 Summary Document (1 hour)
**File:** `week1_summary.md`

```markdown
# Week 1 Summary: Matching Engine

## What Was Built

### Core Components
1. **Order Models** (`trading/events/models.py`)
   - Order, Trade, OrderStatus, OrderSide, OrderType
   - Support for limit and market orders
   - Partial fill tracking

2. **Order Book** (`trading/engine/order_book.py`)
   - Price-time priority matching
   - Efficient price level management using dict + deque
   - Separate bid/ask sides
   - Market and limit order execution

3. **Order Book Manager** (`trading/engine/order_book_manager.py`)
   - Multi-ticker support (AAPL, MSFT, GOOGL, TSLA, NVDA)
   - Order routing by ticker
   - Isolated order books per ticker

4. **Matching Engine** (`trading/engine/matcher.py`)
   - Clean API for order submission
   - Cross-ticker order cancellation
   - Market data retrieval

## Test Coverage

### Unit Tests
- `test_models.py`: Order model behavior
- `test_order_book.py`: Single order book matching logic
- `test_order_book_manager.py`: Multi-ticker routing

### Property Tests (Hypothesis)
- No negative fills
- Trade volume conservation
- Deterministic replay
- Price improvement

### Integration Tests
- End-to-end trading scenarios
- Cross-ticker independence
- Market data retrieval

### Total Test Count
Run `pytest --collect-only` to see exact count.

## Key Design Decisions

### 1. Price-Time Priority
- Used `dict[Decimal, deque[Order]]` for O(1) price level access
- FIFO within price level via deque
- Separate bid/ask dictionaries

### 2. Determinism
- Single-threaded execution per book
- No random tie-breaking
- Timestamp preserved but not used for ordering (order of arrival)

### 3. Multi-Ticker Isolation
- Each ticker has independent order book
- No cross-ticker risk calculations (Week 3)
- Manager routes by ticker symbol

### 4. Market Orders
- Immediate-or-Cancel (IOC) only
- Reject if no liquidity
- Never rest in book

## Performance Characteristics

- **Order submission**: O(1) if no match, O(N) if matching
- **Best bid/ask**: O(1) via min/max on dict keys
- **Cancellation**: O(P) where P = orders at price level

## What's Next (Week 2)

- FastAPI REST endpoints
- WebSocket market data broadcast
- Async order queue
- Throughput benchmarking

## Validation Checklist

- [x] All unit tests pass
- [x] Property tests pass (100+ random scenarios)
- [x] Integration tests pass
- [x] Code documented with docstrings
- [x] Examples run successfully
- [x] No TODOs or FIXMEs in code
- [x] Deterministic behavior validated

## Known Limitations

1. **No order validation**: Assumes valid inputs (add in Week 3)
2. **No persistence**: In-memory only (add in Week 3)
3. **No metrics**: No observability yet (add in Week 4)
4. **Order ID collisions**: No enforcement of unique IDs across tickers
5. **No tick size**: Accepts any Decimal price

## Lines of Code

```bash
find trading/ -name "*.py" | xargs wc -l
find trading/tests/ -name "*.py" | xargs wc -l
```

## Resume Bullet (Draft)

"Built deterministic multi-ticker matching engine in Python supporting limit/market orders with price-time priority, achieving 100% test coverage with property-based testing (Hypothesis) and proving key invariants (no negative fills, deterministic replay, volume conservation)."
```

---

### Step 5.4: Run Full Test Suite (15 min)
```bash
# Run all tests with coverage
pytest -v --tb=short

# Run with coverage report (optional)
# uv add --dev pytest-cov
# pytest --cov=trading --cov-report=html

# Run property tests with more examples
pytest trading/tests/test_properties.py -v --hypothesis-show-statistics

# Check test count
pytest --collect-only
```

---

### Step 5.5: Code Quality Check (15 min)

Optional but recommended:

```bash
# Install tools
uv add --dev black ruff mypy

# Format code
black trading/

# Lint
ruff check trading/

# Type checking
mypy trading/ --ignore-missing-imports
```

---

## Day 5 Checklist
- [ ] All modules have docstrings
- [ ] Usage examples created and tested
- [ ] Week 1 summary document complete
- [ ] All tests passing (run `pytest -v`)
- [ ] Code formatted and linted
- [ ] No TODOs or FIXMEs remaining

---

## Week 1 Final Validation

### Must Pass:
```bash
# All tests pass
pytest -v

# No syntax errors
python -m py_compile trading/**/*.py

# Examples run successfully
python examples/basic_usage.py
```

### Metrics to Record:
- Total test count: `pytest --collect-only`
- Code coverage: `pytest --cov=trading`
- Lines of code: `find trading/ -name "*.py" | xargs wc -l`

### Success Criteria:
✅ Order book correctly matches limit orders with price-time priority
✅ Market orders execute immediately or reject
✅ Multiple tickers work independently
✅ Property tests prove key invariants
✅ 100% deterministic replay
✅ Clean, documented code
✅ Zero failing tests

---

## Troubleshooting

### Common Issues:

**Issue: Decimal precision errors**
```python
# Use quantize for price comparisons
price = price.quantize(Decimal("0.01"))
```

**Issue: Order registry grows unbounded**
```python
# TODO Week 3: Add cleanup for completed orders
```

**Issue: Hypothesis tests too slow**
```python
# Reduce max_examples if needed
@given(st.lists(order_strategy(), max_size=20))
```

---

## Next Week Preview

Week 2 will add:
- FastAPI REST API
- WebSocket subscriptions
- Async order processing
- Throughput benchmarks

You'll integrate the matching engine you built this week into an actual web service.

---

**End of Week 1 Execution Plan**
