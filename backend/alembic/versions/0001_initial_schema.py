"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-10

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

user_role_enum = postgresql.ENUM("passenger", "driver", "admin", name="userrole")
ride_status_enum = postgresql.ENUM(
    "requested",
    "matching",
    "accepted",
    "driver_arriving",
    "in_progress",
    "completed",
    "cancelled",
    "no_drivers_found",
    name="ridestatus",
)


def upgrade() -> None:
    bind = op.get_bind()
    user_role_enum.create(bind, checkfirst=True)
    ride_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("role", user_role_enum, nullable=False, server_default="passenger"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("token_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"])

    op.create_table(
        "driver_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            unique=True,
        ),
        sa.Column("vehicle_make", sa.String(100), server_default=""),
        sa.Column("vehicle_plate", sa.String(50), server_default=""),
        sa.Column("is_available", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("last_lat", sa.Float, nullable=True),
        sa.Column("last_lng", sa.Float, nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rating", sa.Float, nullable=False, server_default="5.0"),
    )

    op.create_table(
        "rides",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("passenger_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id")),
        sa.Column("driver_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("status", ride_status_enum, nullable=False, server_default="requested"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("pickup_lat", sa.Float, nullable=False),
        sa.Column("pickup_lng", sa.Float, nullable=False),
        sa.Column("dropoff_lat", sa.Float, nullable=False),
        sa.Column("dropoff_lng", sa.Float, nullable=False),
        sa.Column("fare_estimate", sa.Float, nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_rides_passenger_id", "rides", ["passenger_id"])
    op.create_index("ix_rides_driver_id", "rides", ["driver_id"])
    op.create_index("ix_rides_status", "rides", ["status"])
    op.create_index("ix_rides_idempotency_key", "rides", ["idempotency_key"])
    op.create_index("ix_rides_passenger_status", "rides", ["passenger_id", "status"])

    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("aggregate_type", sa.String(50), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("published", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_outbox_events_aggregate_id", "outbox_events", ["aggregate_id"])
    op.create_index("ix_outbox_events_published", "outbox_events", ["published"])

    op.create_table(
        "idempotency_records",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("scope", sa.String(100), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("response_body", sa.JSON, nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("key", "scope", name="uq_idempotency_key_scope"),
    )
    op.create_index("ix_idempotency_records_key", "idempotency_records", ["key"])


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_table("outbox_events")
    op.drop_table("rides")
    op.drop_table("driver_profiles")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
    ride_status_enum.drop(op.get_bind(), checkfirst=True)
    user_role_enum.drop(op.get_bind(), checkfirst=True)
