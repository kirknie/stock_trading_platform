"""
WebSocket endpoint for real-time market data.

Protocol:
  1. Client connects to /ws
  2. Client sends: {"action": "subscribe", "tickers": ["AAPL", ...]}
  3. Server streams: book_update and trade events for subscribed tickers
  4. Client disconnects: server cleans up subscription
"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from trading.api.broadcaster import get_broadcaster

logger = logging.getLogger(__name__)
ws_router = APIRouter()


@ws_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    broadcaster = get_broadcaster()
    client_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    subscribed = False

    try:
        # Wait for subscription message
        raw = await websocket.receive_text()
        msg = json.loads(raw)

        if msg.get("action") != "subscribe" or "tickers" not in msg:
            await websocket.send_text(
                json.dumps(
                    {
                        "error": 'First message must be: {"action": "subscribe", "tickers": [...]}'
                    }
                )
            )
            return

        tickers = msg["tickers"]
        broadcaster.subscribe(client_queue, tickers)
        subscribed = True
        logger.info("Client subscribed to: %s", tickers)

        await websocket.send_text(
            json.dumps(
                {
                    "type": "subscribed",
                    "tickers": tickers,
                }
            )
        )

        # Race between broadcaster messages and incoming WebSocket frames
        receive_task = asyncio.create_task(websocket.receive())

        try:
            while True:
                queue_task = asyncio.create_task(client_queue.get())
                done, _ = await asyncio.wait(
                    {queue_task, receive_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if receive_task in done:
                    queue_task.cancel()
                    raw_msg = receive_task.result()
                    if raw_msg["type"] == "websocket.disconnect":
                        break
                    # Any other client message (e.g. ping) is ignored;
                    # re-arm the receive task for the next frame.
                    receive_task = asyncio.create_task(websocket.receive())

                if queue_task in done:
                    payload = queue_task.result()
                    await websocket.send_text(payload)
        finally:
            receive_task.cancel()

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        if subscribed:
            broadcaster.unsubscribe(client_queue)
