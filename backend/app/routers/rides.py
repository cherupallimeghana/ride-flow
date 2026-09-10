"""
Ride lifecycle endpoints.

POST /rides is idempotency-key protected: retried ride requests
(e.g. a mobile client with flaky connectivity retrying a timed-out
POST) must not create duplicate rides.

POST /rides/{id}/transition drives the state machine and, on
successful match, performs the atomic Redis-based driver reservation
so two simultaneous match attempts for the same driver can't both
succeed.
"""
import math

from fastapi import APIRouter, Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_roles
from app.logging_config import get_logger
from app.models import Ride, RideStatus, User, UserRole
from app.redis_client import get_redis
from app.schemas import RideCreateRequest, RideOut, RideTransitionRequest
from app.services.idempotency import fingerprint, get_cached_response, store_response
from app.services.matching import find_and_reserve_driver, release_driver_reservation
from app.services.ride_state_machine import InvalidTransitionError, StaleVersionError, apply_transition

router = APIRouter(prefix="/api/v1/rides", tags=["rides"])
log = get_logger(__name__)

IDEMPOTENCY_SCOPE_CREATE = "create_ride"


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _estimate_fare(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng) -> float:
    base_fare, per_km = 2.5, 1.35
    distance = _haversine_km(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng)
    return round(base_fare + per_km * distance, 2)


@router.post("", response_model=RideOut, status_code=status.HTTP_201_CREATED)
async def create_ride(
    payload: RideCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(require_roles(UserRole.PASSENGER)),
    db: AsyncSession = Depends(get_db),
):
    request_dict = payload.model_dump()

    if idempotency_key:
        cached = await get_cached_response(db, idempotency_key, IDEMPOTENCY_SCOPE_CREATE)
        if cached is not None:
            if cached.request_fingerprint != fingerprint(request_dict):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Idempotency-Key was already used with a different request body",
                )
            log.info("idempotent_replay", key=idempotency_key, scope=IDEMPOTENCY_SCOPE_CREATE)
            return cached.response_body

    fare = _estimate_fare(payload.pickup_lat, payload.pickup_lng, payload.dropoff_lat, payload.dropoff_lng)
    ride = Ride(
        passenger_id=user.id,
        status=RideStatus.REQUESTED,
        pickup_lat=payload.pickup_lat,
        pickup_lng=payload.pickup_lng,
        dropoff_lat=payload.dropoff_lat,
        dropoff_lng=payload.dropoff_lng,
        fare_estimate=fare,
        idempotency_key=idempotency_key,
    )
    db.add(ride)
    await db.flush()
    await db.refresh(ride)

    ride_out = RideOut.model_validate(ride).model_dump(mode="json")

    if idempotency_key:
        await store_response(
            db, idempotency_key, IDEMPOTENCY_SCOPE_CREATE, request_dict, status.HTTP_201_CREATED, ride_out
        )

    log.info("ride_created", ride_id=ride.id, passenger_id=user.id, fare=fare)
    return ride


@router.get("/{ride_id}", response_model=RideOut)
async def get_ride(ride_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    ride = await db.get(Ride, ride_id)
    if ride is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ride not found")
    if user.role == UserRole.PASSENGER and ride.passenger_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your ride")
    if user.role == UserRole.DRIVER and ride.driver_id not in (None, user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your ride")
    return ride


@router.get("", response_model=list[RideOut])
async def list_my_rides(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if user.role == UserRole.DRIVER:
        stmt = select(Ride).where(Ride.driver_id == user.id).order_by(Ride.created_at.desc())
    else:
        stmt = select(Ride).where(Ride.passenger_id == user.id).order_by(Ride.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/{ride_id}/transition", response_model=RideOut)
async def transition_ride(
    ride_id: str,
    payload: RideTransitionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    ride = await db.get(Ride, ride_id)
    if ride is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ride not found")

    is_owner_passenger = user.role == UserRole.PASSENGER and ride.passenger_id == user.id
    is_owner_driver = user.role == UserRole.DRIVER and ride.driver_id == user.id
    is_admin = user.role == UserRole.ADMIN
    is_unassigned_driver_matching = (
        user.role == UserRole.DRIVER and ride.driver_id is None and payload.target_status == RideStatus.ACCEPTED
    )
    if not (is_owner_passenger or is_owner_driver or is_admin or is_unassigned_driver_matching):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to modify this ride")

    try:
        if payload.target_status == RideStatus.MATCHING and ride.status == RideStatus.REQUESTED:
            # Kick off matching synchronously for demo purposes; in a
            # higher-throughput system this would enqueue a Celery task
            # instead of blocking the request. REQUESTED -> MATCHING is
            # itself a legal transition, then we immediately attempt to
            # resolve MATCHING -> ACCEPTED (driver reserved) or
            # MATCHING -> NO_DRIVERS_FOUND, so the caller gets a
            # terminal-for-now status in one round trip.
            matching_result = await apply_transition(db, ride, RideStatus.MATCHING, payload.expected_version)
            ride = matching_result.ride

            driver_id = await find_and_reserve_driver(redis, ride.pickup_lat, ride.pickup_lng, ride.id)
            if driver_id is None:
                final_result = await apply_transition(db, ride, RideStatus.NO_DRIVERS_FOUND, ride.version)
            else:
                final_result = await apply_transition(
                    db, ride, RideStatus.ACCEPTED, ride.version, extra_fields={"driver_id": driver_id}
                )

            log.info(
                "ride_transitioned",
                ride_id=ride.id,
                from_status=RideStatus.REQUESTED.value,
                to_status=final_result.ride.status.value,
            )
            return final_result.ride

        result = await apply_transition(db, ride, payload.target_status, payload.expected_version)

        if payload.target_status == RideStatus.CANCELLED and ride.driver_id:
            await release_driver_reservation(redis, ride.driver_id, ride.id)

        log.info(
            "ride_transitioned",
            ride_id=ride.id,
            from_status=result.previous_status.value,
            to_status=payload.target_status.value,
        )
        return result.ride

    except InvalidTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except StaleVersionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
