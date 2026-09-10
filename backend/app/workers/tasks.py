"""
Background jobs run by Celery workers/beat.

- send_ride_notification: simulates dispatching a push/SMS
  notification (would call a provider like Twilio/FCM in production;
  the README's caveat about sandbox credentials applies here).
- evict_stale_drivers: periodic sweep that removes drivers from the
  Redis GEO matching pool if they haven't sent a heartbeat recently,
  so a client that crashed without cleanly going "unavailable"
  doesn't keep getting matched to rides it will never accept.
- cleanup_idempotency_records: prunes old idempotency cache rows so
  the table doesn't grow unbounded.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import get_settings
from app.database import session_scope
from app.logging_config import get_logger
from app.models import DriverProfile, IdempotencyRecord
from app.redis_client import get_redis
from app.services.matching import remove_driver_from_pool
from app.workers.celery_app import celery_app

settings = get_settings()
log = get_logger(__name__)


def _run_async(coro):
    """Celery tasks are sync; bridge into our async DB/Redis clients."""
    return asyncio.run(coro)


@celery_app.task(name="app.workers.tasks.send_ride_notification", bind=True, max_retries=3)
def send_ride_notification(self, ride_id: str, event_type: str, payload: dict):
    try:
        log.info("notification_dispatched", ride_id=ride_id, event_type=event_type, payload=payload)
        # Real implementation would call an SMS/push provider here.
    except Exception as exc:  # pragma: no cover - illustrative retry path
        raise self.retry(exc=exc, countdown=2**self.request.retries)


@celery_app.task(name="app.workers.tasks.evict_stale_drivers")
def evict_stale_drivers():
    return _run_async(_evict_stale_drivers_async())


async def _evict_stale_drivers_async():
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.driver_stale_after_seconds)
    redis = get_redis()
    evicted = []

    async with session_scope() as db:
        result = await db.execute(
            select(DriverProfile).where(
                DriverProfile.is_available.is_(True),
                DriverProfile.last_heartbeat_at < cutoff,
            )
        )
        stale_drivers = result.scalars().all()
        for driver in stale_drivers:
            driver.is_available = False
            await remove_driver_from_pool(redis, driver.user_id)
            evicted.append(driver.user_id)

    if evicted:
        log.info("stale_drivers_evicted", driver_ids=evicted, count=len(evicted))
    return {"evicted_count": len(evicted)}


@celery_app.task(name="app.workers.tasks.cleanup_idempotency_records")
def cleanup_idempotency_records():
    return _run_async(_cleanup_idempotency_records_async())


async def _cleanup_idempotency_records_async():
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    async with session_scope() as db:
        result = await db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.created_at < cutoff))
        return {"deleted_count": result.rowcount}
