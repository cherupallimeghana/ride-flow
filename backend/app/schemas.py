"""Pydantic schemas for request/response validation (API contracts)."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import RideStatus, UserRole

# ---------- Auth ----------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    role: UserRole = UserRole.PASSENGER


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime


# ---------- Drivers ----------

class HeartbeatRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class AvailabilityRequest(BaseModel):
    is_available: bool


class DriverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    is_available: bool
    last_lat: float | None
    last_lng: float | None
    rating: float


# ---------- Rides ----------

class RideCreateRequest(BaseModel):
    pickup_lat: float = Field(ge=-90, le=90)
    pickup_lng: float = Field(ge=-180, le=180)
    dropoff_lat: float = Field(ge=-90, le=90)
    dropoff_lng: float = Field(ge=-180, le=180)


class RideTransitionRequest(BaseModel):
    target_status: RideStatus
    expected_version: int = Field(description="Optimistic concurrency token from the last read of the ride")


class RideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    passenger_id: str
    driver_id: str | None
    status: RideStatus
    version: int
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float
    fare_estimate: float | None
    created_at: datetime
    updated_at: datetime


class ErrorResponse(BaseModel):
    detail: str
    error_code: str | None = None
