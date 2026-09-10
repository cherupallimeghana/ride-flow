"""
Geospatial driver discovery + atomic reservation.

Discovery: driver locations live in a Redis GEO set (`GEOADD`) so we
can ask "who is within N km of the pickup point" in O(log N) via
`GEOSEARCH`, without hitting Postgres for a heavy spatial query on
every ride request.

Reservation: the hard part of matching is that two ride requests
might both try to grab the same nearby driver at the same instant.
A plain "check is_available, then set is_available=false" is a
classic race condition (read-then-write, not atomic). We solve this
with Redis `SET key value NX EX ttl` -- itself a single atomic
command (not a compound read-then-write), so only one of two
concurrent callers can ever have it succeed. The TTL means a crashed
matcher doesn't strand a driver reserved forever.
"""
from redis.asyncio import Redis

from app.config import get_settings

settings = get_settings()

DRIVER_GEO_KEY = "drivers:geo"
DRIVER_RESERVATION_PREFIX = "driver:reservation:"


async def upsert_driver_location(redis: Redis, driver_id: str, lat: float, lng: float) -> None:
    await redis.geoadd(DRIVER_GEO_KEY, (lng, lat, driver_id))


async def remove_driver_from_pool(redis: Redis, driver_id: str) -> None:
    await redis.zrem(DRIVER_GEO_KEY, driver_id)


async def find_nearby_drivers(
    redis: Redis, lat: float, lng: float, radius_km: float | None = None, count: int = 10
) -> list[str]:
    radius_km = radius_km or settings.driver_search_radius_km
    results = await redis.geosearch(
        DRIVER_GEO_KEY,
        longitude=lng,
        latitude=lat,
        radius=radius_km,
        unit="km",
        sort="ASC",  # nearest first
        count=count,
    )
    return list(results)


async def try_reserve_driver(redis: Redis, driver_id: str, ride_id: str) -> bool:
    """
    Atomically reserve a driver for a ride using SET-if-not-exists.
    `nx=True` makes this a single indivisible check-and-set at the
    Redis protocol level -- no separate read then write, so two
    concurrent callers can never both succeed for the same driver.
    Returns True on success.
    """
    result = await redis.set(
        f"{DRIVER_RESERVATION_PREFIX}{driver_id}",
        ride_id,
        nx=True,
        ex=settings.driver_reservation_ttl_seconds,
    )
    return bool(result)


async def release_driver_reservation(redis: Redis, driver_id: str, ride_id: str) -> bool:
    """
    Release a reservation, but only if it still belongs to this ride.
    This check-then-delete is not perfectly atomic (a theoretical
    window exists between GET and DEL), but the blast radius is
    bounded by the short reservation TTL, and this only runs on the
    ride-cancellation path, not the hot matching path -- an
    acceptable tradeoff against the complexity of a Lua script for a
    portfolio-scope project.
    """
    key = f"{DRIVER_RESERVATION_PREFIX}{driver_id}"
    current = await redis.get(key)
    if current == ride_id:
        await redis.delete(key)
        return True
    return False


async def find_and_reserve_driver(redis: Redis, lat: float, lng: float, ride_id: str) -> str | None:
    """
    Scans nearby drivers in distance order and attempts to reserve
    the first one that isn't already claimed. Returns driver_id or
    None if no driver in range could be reserved.
    """
    candidates = await find_nearby_drivers(redis, lat, lng)
    for driver_id in candidates:
        if await try_reserve_driver(redis, driver_id, ride_id):
            return driver_id
    return None

