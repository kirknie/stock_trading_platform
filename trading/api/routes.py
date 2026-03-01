"""
REST API routes for the trading platform.

Endpoints:
  POST   /orders              — Submit a new order
  POST   /orders/{id}/cancel  — Cancel an open order
  GET    /book/{ticker}       — Get order book snapshot
  GET    /tickers             — List supported tickers
"""

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from trading.api.dependencies import get_engine, get_event_log, get_idempotency_store, get_order_queue, get_risk
from trading.api.dependencies import IdempotencyStore
from trading.persistence.event_log import EventLog
from trading.risk.checker import RiskChecker, RiskViolation
from trading.api.schemas import (
    CancelResponse,
    OrderBookLevel,
    OrderBookResponse,
    OrderRequest,
    OrderResponse,
    TickersResponse,
    TradeResponse,
)
from trading.engine.matcher import MatchingEngine
from trading.events.models import Order

router = APIRouter()


def _build_order_book_response(
    engine: MatchingEngine, ticker: str
) -> OrderBookResponse:
    """Build a full OrderBookResponse from the live order book."""
    book = engine.manager.get_order_book(ticker)

    # Bids: sort descending (highest price first)
    bids = sorted(
        [
            OrderBookLevel(
                price=price,
                quantity=sum(o.remaining_quantity() for o in queue),
            )
            for price, queue in book.bids.items()
            if queue
        ],
        key=lambda x: x.price,
        reverse=True,
    )

    # Asks: sort ascending (lowest price first)
    asks = sorted(
        [
            OrderBookLevel(
                price=price,
                quantity=sum(o.remaining_quantity() for o in queue),
            )
            for price, queue in book.asks.items()
            if queue
        ],
        key=lambda x: x.price,
    )

    return OrderBookResponse(
        ticker=ticker,
        bids=bids,
        asks=asks,
        best_bid=book.get_best_bid(),
        best_ask=book.get_best_ask(),
        spread=book.get_spread(),
    )


@router.post(
    "/orders",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a new order",
)
async def submit_order(
    request: OrderRequest,
    engine: MatchingEngine = Depends(get_engine),
    queue: asyncio.Queue = Depends(get_order_queue),
    idempotency: IdempotencyStore = Depends(get_idempotency_store),
) -> OrderResponse:
    """
    Submit a limit or market order.

    If a client-supplied order_id is provided and has been seen before,
    the cached response is returned immediately (HTTP 200) without creating
    a duplicate order. New orders return HTTP 201.

    The order is placed on the async queue and processed by the
    background consumer. The response includes any trades generated.
    """
    # Idempotency check — must come before any side effects
    if request.order_id is not None and request.order_id in idempotency:
        return JSONResponse(
            content=idempotency.get(request.order_id),
            status_code=status.HTTP_200_OK,
        )

    if request.ticker not in engine.get_supported_tickers():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Ticker '{request.ticker}' is not supported. "
            f"Supported: {engine.get_supported_tickers()}",
        )

    order = Order(
        order_id=request.order_id or str(uuid.uuid4()),
        ticker=request.ticker,
        side=request.side,
        order_type=request.order_type,
        quantity=request.quantity,
        price=request.price,
        timestamp=datetime.now(tz=timezone.utc),
        account_id=request.account_id,
    )

    # Enqueue order and await result from consumer
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    await queue.put((order, future))
    try:
        trades = await future
    except RiskViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        )

    response = OrderResponse(
        order_id=order.order_id,
        ticker=order.ticker,
        side=order.side,
        order_type=order.order_type,
        quantity=order.quantity,
        price=order.price,
        status=order.status,
        filled_quantity=order.filled_quantity,
        trades=[
            TradeResponse(
                trade_id=t.trade_id,
                ticker=t.ticker,
                buyer_order_id=t.buyer_order_id,
                seller_order_id=t.seller_order_id,
                price=t.price,
                quantity=t.quantity,
                timestamp=t.timestamp,
            )
            for t in trades
        ],
    )

    # Cache for future duplicate submissions
    if request.order_id is not None:
        idempotency.store(request.order_id, response.model_dump(mode="json"))

    return response


@router.post(
    "/orders/{order_id}/cancel",
    response_model=CancelResponse,
    summary="Cancel an open order",
)
async def cancel_order(
    order_id: str,
    engine: MatchingEngine = Depends(get_engine),
    risk: RiskChecker = Depends(get_risk),
    event_log: EventLog = Depends(get_event_log),
) -> CancelResponse:
    """
    Cancel an open order by its ID.

    Cancellation is synchronous (no queue) because it does not
    generate trades and must not be reordered with submissions.
    """
    ticker_and_order = engine.order_registry.get(order_id)
    success = engine.cancel_order(order_id)
    if success and ticker_and_order is not None:
        _, order = ticker_and_order
        risk.record_cancel(order)
        await event_log.append_order_cancelled(order_id, order.ticker)
    return CancelResponse(
        order_id=order_id,
        success=success,
        message="Order canceled" if success else "Order not found or already completed",
    )


@router.get(
    "/book/{ticker}",
    response_model=OrderBookResponse,
    summary="Get order book snapshot",
)
async def get_order_book(
    ticker: str,
    engine: MatchingEngine = Depends(get_engine),
) -> OrderBookResponse:
    """
    Return a snapshot of the current order book for the given ticker.

    Includes all visible bids and asks aggregated by price level.
    """
    if ticker not in engine.get_supported_tickers():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticker '{ticker}' not found. "
            f"Supported: {engine.get_supported_tickers()}",
        )
    return _build_order_book_response(engine, ticker)


@router.get(
    "/tickers",
    response_model=TickersResponse,
    summary="List supported tickers",
)
async def list_tickers(
    engine: MatchingEngine = Depends(get_engine),
) -> TickersResponse:
    """Return the list of all supported tickers."""
    return TickersResponse(tickers=engine.get_supported_tickers())
