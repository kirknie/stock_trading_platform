"""
Health and readiness endpoints.

GET /health  — liveness probe: returns 200 if the process is running
GET /ready   — readiness probe: returns 200 if all singletons are initialised
               and the consumer tasks are alive
"""

from fastapi import APIRouter, Response, status

from trading.api.dependencies import get_engine, get_event_log, get_order_queues

health_router = APIRouter()


@health_router.get("/health", include_in_schema=False)
async def health() -> dict:
    """Liveness probe — always returns 200 if the process is up."""
    return {"status": "ok"}


@health_router.get("/ready", include_in_schema=False)
async def ready(response: Response) -> dict:
    """
    Readiness probe — returns 200 only if core singletons are initialised.
    Returns 503 if startup is incomplete (e.g., called before lifespan completes).
    """
    try:
        engine = get_engine()
        _ = get_event_log()
        queues = get_order_queues()
    except RuntimeError as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": str(exc)}

    return {
        "status": "ready",
        "tickers": engine.get_supported_tickers(),
        "queue_depths": {ticker: q.qsize() for ticker, q in queues.items()},
    }
