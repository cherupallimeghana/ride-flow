"""
Transactional outbox poller.

Runs as its own long-lived process (see Dockerfile / docker-compose
`outbox-worker` service). Every `poll_interval_seconds` it:

  1. Selects a batch of unpublished OutboxEvent rows, locked with
     `FOR UPDATE SKIP LOCKED` so multiple worker replicas can run
     concurrently without double-processing the same row.
  2. Publishes each event to the ride's Redis pub/sub channel (for
     live WebSocket subscribers) and enqueues a Celery notification
     task (for push/SMS).
  3. Marks the row published in the same transaction that read it.

This is what closes the loop opened by services/ride_state_machine.py
writing OutboxEvent rows atomically with the ride's status change:
the event is guaranteed to eventually be delivered exactly because
it was durably committed to Postgres first, independent of whether
Redis/Celery were reachable at the moment of the original request.
"""
import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import session_scope
from app.logging_config import configure_logging, get_logger
from app.models import OutboxEvent
from app.redis_client import get_redis
from app.workers.tasks import send_ride_notification

log = get_logger(__name__)

POLL_INTERVAL_SECONDS = 1.0
BATCH_SIZE = 50


async def process_batch() -> int:
    redis = get_redis()
    async with session_scope() as db:
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.published.is_(False))
            .order_by(OutboxEvent.created_at)
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        result = await db.execute(stmt)
        events = result.scalars().all()

        for event in events:
            channel = f"ride:{event.aggregate_id}:events"
            await redis.publish(
                channel,
                json.dumps(
                    {
                        "event_type": event.event_type,
                        "aggregate_id": event.aggregate_id,
                        "payload": event.payload,
                        "created_at": event.created_at.isoformat() if event.created_at else None,
                    }
                ),
            )
            send_ride_notification.delay(event.aggregate_id, event.event_type, event.payload)

            event.published = True
            event.published_at = datetime.now(timezone.utc)

        return len(events)


async def run_forever():
    configure_logging()
    log.info("outbox_worker_started", poll_interval=POLL_INTERVAL_SECONDS)
    while True:
        try:
            processed = await process_batch()
            if processed:
                log.info("outbox_batch_processed", count=processed)
        except Exception:
            log.exception("outbox_worker_error")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_forever())
