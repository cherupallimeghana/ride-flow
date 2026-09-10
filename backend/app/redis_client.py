"""Shared async Redis connection pool used for geo-matching, rate
limiting, driver reservation locks, and pub/sub."""
from redis import asyncio as aioredis

from app.config import get_settings

settings = get_settings()

redis_pool = aioredis.ConnectionPool.from_url(settings.redis_url, decode_responses=True)


def get_redis() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=redis_pool)
