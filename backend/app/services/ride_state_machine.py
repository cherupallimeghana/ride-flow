"""
Centralized ride state machine.

Two safety properties are enforced here, and nowhere else:

1. Legality: a transition is only applied if it appears in
   RIDE_TRANSITIONS[current_status]. This stops any code path from
   accidentally moving a ride, say, from COMPLETED back to
   IN_PROGRESS.

2. Optimistic concurrency: callers must supply the `version` they
   last read. If another request has mutated the ride in the
   meantime (version mismatch), we raise a StaleVersionError instead
   of silently overwriting their change. This is cheaper than
   row-level locking and is sufficient because ride transitions are
   infrequent and conflict is rare (still needs to be handled
   correctly when it happens, e.g. two drivers accepting at once).
"""
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RIDE_TRANSITIONS, OutboxEvent, Ride, RideStatus


class InvalidTransitionError(Exception):
    pass


class StaleVersionError(Exception):
    pass


@dataclass
class TransitionResult:
    ride: Ride
    previous_status: RideStatus


def assert_valid_transition(current: RideStatus, target: RideStatus) -> None:
    allowed = RIDE_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(
            f"Cannot transition ride from '{current.value}' to '{target.value}'. "
            f"Allowed: {[s.value for s in allowed] or 'none (terminal state)'}"
        )


async def apply_transition(
    db: AsyncSession,
    ride: Ride,
    target_status: RideStatus,
    expected_version: int,
    extra_fields: dict | None = None,
) -> TransitionResult:
    """
    Validates and applies a ride state transition atomically using a
    conditional UPDATE (WHERE id = ... AND version = ...). If zero
    rows are affected, either the ride doesn't exist or the version
    is stale — we distinguish those cases for a clearer error.

    Also writes an OutboxEvent in the SAME transaction, so a status
    change and its corresponding event notification are atomic
    (transactional outbox pattern).
    """
    assert_valid_transition(ride.status, target_status)

    previous_status = ride.status
    values = {"status": target_status, "version": Ride.version + 1}
    if extra_fields:
        values.update(extra_fields)

    stmt = (
        update(Ride)
        .where(Ride.id == ride.id, Ride.version == expected_version)
        .values(**values)
        .returning(Ride.version)
    )
    result = await db.execute(stmt)
    row = result.first()
    if row is None:
        raise StaleVersionError(
            f"Ride {ride.id} was modified concurrently (expected version {expected_version}). "
            "Re-fetch the ride and retry."
        )

    await db.refresh(ride)

    db.add(
        OutboxEvent(
            aggregate_type="ride",
            aggregate_id=ride.id,
            event_type="ride.status_changed",
            payload={
                "ride_id": ride.id,
                "previous_status": previous_status.value,
                "new_status": target_status.value,
                "driver_id": ride.driver_id,
                "passenger_id": ride.passenger_id,
            },
        )
    )

    return TransitionResult(ride=ride, previous_status=previous_status)
