"""
Driver-facing endpoints: heartbeat (location + liveness) and
availability toggling.

A driver is only matchable while (a) is_available=true AND (b) they
have sent a heartbeat recently. Heartbeats update both Postgres
(durable record) and the Redis GEO set (fast lookup). A separate
eviction job (workers/tasks.py::evict_stale_drivers) removes drivers
from the Redis pool if their heartbeat goes stale, so a driver who
force-quits the app without deregistering doesn't stay "available"
forever.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_roles
from app.models import DriverProfile, User, UserRole
from app.redis_client import get_redis
from app.schemas import AvailabilityRequest, DriverOut, HeartbeatRequest
from app.services.matching import remove_driver_from_pool, upsert_driver_location

router = APIRouter(prefix="/api/v1/drivers", tags=["drivers"])


async def _get_or_create_profile(db: AsyncSession, user: User) -> DriverProfile:
    # Query directly rather than via `user.driver_profile`: the user
    # object here comes from a plain `select(User)` (no eager-load of
    # relationships), so touching a lazy relationship attribute on an
    # AsyncSession object outside of an awaited ORM call raises
    # MissingGreenlet. An explicit select avoids that entirely.
    result = await db.execute(select(DriverProfile).where(DriverProfile.user_id == user.id))
    profile = result.scalar_one_or_none()
    if profile is not None:
        return profile
    profile = DriverProfile(user_id=user.id)
    db.add(profile)
    await db.flush()
    return profile


@router.post("/me/heartbeat", response_model=DriverOut)
async def heartbeat(
    payload: HeartbeatRequest,
    user: User = Depends(require_roles(UserRole.DRIVER)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    profile = await _get_or_create_profile(db, user)
    profile.last_lat = payload.lat
    profile.last_lng = payload.lng
    profile.last_heartbeat_at = datetime.now(timezone.utc)

    if profile.is_available:
        await upsert_driver_location(redis, user.id, payload.lat, payload.lng)

    return profile


@router.post("/me/availability", response_model=DriverOut)
async def set_availability(
    payload: AvailabilityRequest,
    user: User = Depends(require_roles(UserRole.DRIVER)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    profile = await _get_or_create_profile(db, user)
    profile.is_available = payload.is_available

    if payload.is_available:
        if profile.last_lat is None or profile.last_lng is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Send a heartbeat with your location before going available",
            )
        await upsert_driver_location(redis, user.id, profile.last_lat, profile.last_lng)
    else:
        await remove_driver_from_pool(redis, user.id)

    return profile


@router.get("/me", response_model=DriverOut)
async def get_my_profile(
    user: User = Depends(require_roles(UserRole.DRIVER)),
    db: AsyncSession = Depends(get_db),
):
    return await _get_or_create_profile(db, user)
