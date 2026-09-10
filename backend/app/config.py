"""
Centralized application configuration.

All environment-driven settings live here so the rest of the codebase
never touches os.environ directly. This makes behavior deterministic
and testable (settings can be overridden via env vars or a .env file).
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- App ---
    app_name: str = "RideFlow"
    environment: str = "development"
    debug: bool = True

    # --- Database ---
    database_url: str = "postgresql+asyncpg://rideflow:rideflow@localhost:5432/rideflow"
    database_url_sync: str = "postgresql+psycopg2://rideflow:rideflow@localhost:5432/rideflow"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Security ---
    jwt_secret_key: str = "CHANGE_ME_IN_PRODUCTION"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_minutes: int = 60 * 24 * 7  # 7 days

    # --- Rate limiting ---
    rate_limit_per_minute: int = 60

    # --- Driver matching ---
    driver_search_radius_km: float = 5.0
    driver_stale_after_seconds: int = 30
    driver_reservation_ttl_seconds: int = 15

    # --- Celery ---
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"


@lru_cache
def get_settings() -> Settings:
    return Settings()
