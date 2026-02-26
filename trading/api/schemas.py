"""
Pydantic schemas for REST API request and response bodies.

Keep these separate from domain models to allow independent evolution
of the API contract vs internal data structures.
"""

from decimal import Decimal
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, model_validator

from trading.events.models import OrderSide, OrderType, OrderStatus

# ── Request Schemas ──────────────────────────────────────────────────────────


class OrderRequest(BaseModel):
    """Request body for POST /orders."""

    ticker: str = Field(..., min_length=1, max_length=10, examples=["AAPL"])
    side: OrderSide
    order_type: OrderType
    quantity: int = Field(..., gt=0, examples=[100])
    price: Optional[Decimal] = Field(
        None,
        gt=0,
        decimal_places=2,
        examples=["150.00"],
        description="Required for LIMIT orders. Omit for MARKET orders.",
    )
    account_id: str = Field(default="default", min_length=1, max_length=50)

    @model_validator(mode="after")
    def price_required_for_limit(self) -> "OrderRequest":
        if self.order_type == OrderType.LIMIT and self.price is None:
            raise ValueError("price is required for LIMIT orders")
        if self.order_type == OrderType.MARKET and self.price is not None:
            raise ValueError("price must be omitted for MARKET orders")
        return self


# ── Response Schemas ─────────────────────────────────────────────────────────


class TradeResponse(BaseModel):
    """Single trade that occurred when an order matched."""

    trade_id: str
    ticker: str
    buyer_order_id: str
    seller_order_id: str
    price: Decimal
    quantity: int
    timestamp: datetime


class OrderResponse(BaseModel):
    """Response after submitting an order."""

    order_id: str
    ticker: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Optional[Decimal]
    status: OrderStatus
    filled_quantity: int
    trades: list[TradeResponse]


class CancelResponse(BaseModel):
    """Response after canceling an order."""

    order_id: str
    success: bool
    message: str


class OrderBookLevel(BaseModel):
    """One price level in the order book."""

    price: Decimal
    quantity: int


class OrderBookResponse(BaseModel):
    """Full order book snapshot for a ticker."""

    ticker: str
    bids: list[OrderBookLevel]  # sorted descending (best bid first)
    asks: list[OrderBookLevel]  # sorted ascending (best ask first)
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    spread: Optional[Decimal]


class TickersResponse(BaseModel):
    """List of supported tickers."""

    tickers: list[str]
