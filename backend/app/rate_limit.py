"""
Lightweight fixed-window rate limiter backed by Redis `INCR` + `EXPIRE`.

Keyed by client IP + current-minute bucket. Simpler than a sliding
window / token bucket, which is a deliberate tradeoff: it can allow a
short burst at window boundaries, but it's O(1), needs no Lua script,
and is easy to reason about — appropriate for a portfolio project
demonstrating the *pattern*, not a hardened production limiter.
"""
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.redis_client import get_redis

settings = get_settings()


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/health"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        window = int(time.time() // 60)
        key = f"ratelimit:{client_ip}:{window}"

        redis = get_redis()
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, 60)

        if current > settings.rate_limit_per_minute:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again shortly."},
                headers={"Retry-After": "60"},
            )

        return await call_next(request)
