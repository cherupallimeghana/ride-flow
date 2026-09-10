"""
Kubernetes/Docker-style health and readiness probes.

- /health/live: is the process up at all (no dependency checks).
- /health/ready: are we actually able to serve traffic (DB + Redis
  reachable). Load balancers/orchestrators should route traffic based
  on this, not /live.
"""
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.redis_client import get_redis

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness():
    return {"status": "alive"}


@router.get("/ready")
async def readiness(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    checks = {"database": False, "redis": False}

    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        pass

    try:
        await redis.ping()
        checks["redis"] = True
    except Exception:
        pass

    healthy = all(checks.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ready" if healthy else "not_ready", "checks": checks},
    )
