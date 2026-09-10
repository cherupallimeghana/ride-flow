"""
WebSocket gateway for real-time ride status/location updates.

Connections subscribe to a per-ride Redis pub/sub channel
(`ride:{ride_id}:events`). The outbox worker (workers/outbox_worker.py)
publishes to that channel whenever a ride's state changes, so any
number of app-server processes can fan events out to connected
clients without needing sticky sessions or direct process-to-process
communication.
"""
import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from app.logging_config import get_logger
from app.redis_client import get_redis
from app.security import decode_token

router = APIRouter()
log = get_logger(__name__)


def ride_channel(ride_id: str) -> str:
    return f"ride:{ride_id}:events"


@router.websocket("/ws/rides/{ride_id}")
async def ride_updates(websocket: WebSocket, ride_id: str, token: str):
    """
    Client connects with `?token=<access_token>`. We validate the JWT
    manually here since WebSocket handshakes can't carry the usual
    Authorization header/Depends chain the same way HTTP routes can.
    """
    try:
        decode_token(token)
    except ValueError:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    redis: Redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(ride_channel(ride_id))

    log.info("ws_connected", ride_id=ride_id)

    async def forward_messages():
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"])

    forward_task = asyncio.create_task(forward_messages())
    try:
        while True:
            # We don't expect inbound client messages beyond keepalive
            # pings, but reading keeps the disconnect detectable.
            await websocket.receive_text()
    except WebSocketDisconnect:
        log.info("ws_disconnected", ride_id=ride_id)
    finally:
        forward_task.cancel()
        await pubsub.unsubscribe(ride_channel(ride_id))
        await pubsub.close()


async def publish_ride_event(redis: Redis, ride_id: str, event: dict) -> None:
    await redis.publish(ride_channel(ride_id), json.dumps(event))
