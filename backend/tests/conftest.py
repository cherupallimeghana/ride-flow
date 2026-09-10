"""
Test fixtures.

We swap the real Postgres/Redis dependencies for lightweight
in-process fakes so the suite runs fast and requires no external
services (useful for CI). SQLite in aiosqlite mode stands in for
Postgres for ORM-level tests; a tiny in-memory fake replaces Redis
for geo/reservation logic tests where we don't need real GEO
commands.
"""
import fakeredis.aioredis
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.rate_limit as rate_limit_module
import app.redis_client as redis_client_module
from app.database import Base, get_db
from app.main import app
from app.models import User, UserRole
from app.security import hash_password

_fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

# Capture the *original* get_redis function object now, before any
# monkeypatching happens below. FastAPI's `dependency_overrides` dict
# is keyed by the exact callable object each route's `Depends(...)`
# was defined with (captured at router import time), so the override
# key must be that same original object -- not whatever
# `redis_client_module.get_redis` happens to point to *after* it's
# been monkeypatched.
_original_get_redis = redis_client_module.get_redis


@pytest_asyncio.fixture(autouse=True)
async def patch_redis(monkeypatch):
    """
    Swap the real Redis client for an in-memory fake so the suite has
    no external service dependency. fakeredis supports GEOADD/
    GEOSEARCH and Lua `EVAL`, which is what our matching/reservation
    logic relies on. Route dependencies (Depends(get_redis)) are
    handled via dependency_overrides in the `client` fixture below;
    this fixture only needs to cover the rate-limit middleware, which
    calls get_redis() directly rather than through FastAPI's DI.
    """
    monkeypatch.setattr(rate_limit_module, "get_redis", lambda: _fake_redis)
    yield
    await _fake_redis.flushall()


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_maker = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_engine):
    session_maker = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[_original_get_redis] = lambda: _fake_redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def passenger_user(db_session):
    user = User(
        email="passenger@example.com",
        hashed_password=hash_password("password123"),
        full_name="Test Passenger",
        role=UserRole.PASSENGER,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user
