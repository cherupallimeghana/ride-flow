"""
ORM models.

Design notes:
- `Ride.version` is an optimistic-concurrency column: every state
  transition must match the version it read, and increments it.
  This prevents two concurrent transition requests from silently
  clobbering each other (classic lost-update problem).
- `OutboxEvent` implements the transactional outbox pattern: domain
  events are written to this table in the SAME DB transaction as the
  business change, then a separate worker polls and publishes them.
  This guarantees we never "lose" an event due to a crash between a
  DB commit and a message publish.
- `IdempotencyKey` stores request fingerprints + cached responses so
  retried POST requests (e.g. ride creation, payment capture) are
  safe to repeat.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


class UserRole(str, enum.Enum):
    PASSENGER = "passenger"
    DRIVER = "driver"
    ADMIN = "admin"


class RideStatus(str, enum.Enum):
    REQUESTED = "requested"        # passenger created the ride, searching for a driver
    MATCHING = "matching"          # actively searching / reserving a driver
    ACCEPTED = "accepted"          # a driver has been reserved & accepted
    DRIVER_ARRIVING = "driver_arriving"
    IN_PROGRESS = "in_progress"    # trip started
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_DRIVERS_FOUND = "no_drivers_found"


# Valid state transitions for the ride state machine.
# Enforced centrally in services/ride_state_machine.py, not scattered
# across route handlers, so the rules can't drift.
RIDE_TRANSITIONS: dict[RideStatus, set[RideStatus]] = {
    RideStatus.REQUESTED: {RideStatus.MATCHING, RideStatus.CANCELLED},
    RideStatus.MATCHING: {RideStatus.ACCEPTED, RideStatus.NO_DRIVERS_FOUND, RideStatus.CANCELLED},
    RideStatus.ACCEPTED: {RideStatus.DRIVER_ARRIVING, RideStatus.CANCELLED},
    RideStatus.DRIVER_ARRIVING: {RideStatus.IN_PROGRESS, RideStatus.CANCELLED},
    RideStatus.IN_PROGRESS: {RideStatus.COMPLETED, RideStatus.CANCELLED},
    RideStatus.COMPLETED: set(),
    RideStatus.CANCELLED: set(),
    RideStatus.NO_DRIVERS_FOUND: {RideStatus.MATCHING, RideStatus.CANCELLED},
}


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False, default=UserRole.PASSENGER)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    driver_profile: Mapped["DriverProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RefreshToken(Base):
    """
    Persisted refresh tokens support rotation + revocation.
    Storing a hash (not the raw token) means a DB leak doesn't leak
    usable tokens.
    """
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")


class DriverProfile(Base):
    __tablename__ = "driver_profiles"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    vehicle_make: Mapped[str] = mapped_column(String(100), default="")
    vehicle_plate: Mapped[str] = mapped_column(String(50), default="")
    is_available: Mapped[bool] = mapped_column(default=False)
    last_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rating: Mapped[float] = mapped_column(Float, default=5.0)

    user: Mapped["User"] = relationship(back_populates="driver_profile")


class Ride(Base):
    __tablename__ = "rides"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    passenger_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    driver_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    status: Mapped[RideStatus] = mapped_column(Enum(RideStatus), default=RideStatus.REQUESTED, index=True)
    version: Mapped[int] = mapped_column(default=1)  # optimistic concurrency control

    pickup_lat: Mapped[float] = mapped_column(Float)
    pickup_lng: Mapped[float] = mapped_column(Float)
    dropoff_lat: Mapped[float] = mapped_column(Float)
    dropoff_lng: Mapped[float] = mapped_column(Float)

    fare_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_rides_passenger_status", "passenger_id", "status"),
    )


class OutboxEvent(Base):
    """
    Transactional outbox: written atomically with the business change.
    A background worker (workers/outbox_worker.py) polls `published =
    false` rows, delivers them (e.g. WebSocket push, Celery task,
    notification), then marks them published.
    """
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    aggregate_type: Mapped[str] = mapped_column(String(50))   # e.g. "ride"
    aggregate_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(100))      # e.g. "ride.status_changed"
    payload: Mapped[dict] = mapped_column(JSON)
    published: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyRecord(Base):
    """
    Caches the outcome of a POST identified by an Idempotency-Key
    header so retries (network blips, client retries) don't double-
    create rides or double-charge payments.
    """
    __tablename__ = "idempotency_records"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    key: Mapped[str] = mapped_column(String(255), index=True)
    scope: Mapped[str] = mapped_column(String(100))  # e.g. "create_ride"
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    response_body: Mapped[dict] = mapped_column(JSON)
    status_code: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("key", "scope", name="uq_idempotency_key_scope"),
    )
