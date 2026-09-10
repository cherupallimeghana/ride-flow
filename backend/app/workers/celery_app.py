"""
Celery application instance for async/background jobs: fare
recalculation, notification dispatch, stale-driver eviction, etc.
Kept separate from the outbox worker (which is a lightweight polling
loop, not a Celery task) so outbox delivery has predictable low
latency independent of the Celery broker's queue depth.
"""
from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "rideflow",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)

celery_app.conf.beat_schedule = {
    "evict-stale-drivers-every-15s": {
        "task": "app.workers.tasks.evict_stale_drivers",
        "schedule": 15.0,
    },
    "cleanup-expired-idempotency-records-daily": {
        "task": "app.workers.tasks.cleanup_idempotency_records",
        "schedule": crontab(hour=3, minute=0),
    },
}
