# RideFlow — Production-Oriented Ride Booking Platform

A portfolio-grade ride booking platform demonstrating backend engineering
fundamentals: authentication & RBAC, a validated state machine, optimistic
concurrency control, idempotent APIs, geospatial matching, atomic
reservation under race conditions, real-time updates over WebSockets, the
transactional outbox pattern, background jobs, structured logging, health
probes, database migrations, automated tests, and CI.

> **Honest scope note:** this is a portfolio implementation, not a production
> ride-hailing system. It runs entirely with local, self-hosted infrastructure
> (Postgres, Redis) and stubs out third-party integrations (SMS/push
> notifications, payments, maps) that a real product would need. See
> [Caveats](#caveats--whats-stubbed) below before describing any of those
> integrations as "done" on a resume.

---

## Architecture

```
React + TypeScript (Vite)
        |
        v
FastAPI API  <----->  WebSocket Gateway (/ws/rides/{id})
        |
        +---- PostgreSQL (+PostGIS image, spatial-ready)
        +---- Redis: GEO driver index, reservation locks, rate limits, pub/sub
        +---- Celery workers + beat (background jobs, stale-driver eviction)
        +---- Outbox poller (transactional outbox -> Redis pub/sub + Celery)
```

Everything runs as separate containers under Docker Compose: `api`,
`outbox-worker`, `celery-worker`, `celery-beat`, `postgres`, `redis`,
`frontend`, plus a one-shot `migrate` service that runs Alembic before the
API starts.

## Core engineering features

| Area | What's implemented | Where |
|---|---|---|
| Auth | JWT access + refresh tokens, refresh rotation with reuse detection, bcrypt password hashing | `app/security.py`, `app/routers/auth.py` |
| RBAC | passenger / driver / admin roles enforced via a dependency guard | `app/dependencies.py` |
| Ride state machine | Explicit transition graph; illegal transitions rejected with 422 | `app/services/ride_state_machine.py` |
| Concurrency control | Optimistic locking via a `version` column + conditional `UPDATE ... WHERE version = ?`; conflicting writers get 409, not silent data loss | `app/services/ride_state_machine.py` |
| Idempotency | `Idempotency-Key` header on ride creation; replays cached response, rejects key reuse with a different body | `app/services/idempotency.py` |
| Geospatial matching | Redis `GEOADD`/`GEOSEARCH` for nearest-driver lookup | `app/services/matching.py` |
| Atomic reservation | `SET key value NX EX ttl` — a single atomic Redis command — prevents two ride requests from double-booking the same driver | `app/services/matching.py` |
| Real-time updates | WebSocket gateway subscribed to a per-ride Redis pub/sub channel | `app/routers/websocket.py` |
| Transactional outbox | Domain events written atomically with the DB change; a separate poller delivers them exactly because they were durably committed first | `app/models.py::OutboxEvent`, `app/workers/outbox_worker.py` |
| Background jobs | Celery + beat: stale-driver eviction, notification dispatch, idempotency-record cleanup | `app/workers/` |
| Rate limiting | Redis fixed-window limiter middleware | `app/rate_limit.py` |
| Structured logging | JSON-capable structured logs via `structlog` | `app/logging_config.py` |
| Health checks | `/health/live` (process up) vs `/health/ready` (DB + Redis reachable) | `app/routers/health.py` |
| Migrations | Alembic, async-engine-aware `env.py`, hand-reviewed initial schema | `backend/alembic/` |
| Tests | Pytest + httpx `AsyncClient`, in-memory SQLite + `fakeredis`, no external services required to run | `backend/tests/` |
| CI | GitHub Actions: lint (ruff), migrate against real Postgres, test against real Postgres + Redis services, frontend build, Docker build | `.github/workflows/ci.yml` |

## Resume-ready description

> **RideFlow — Real-Time Ride Booking Platform**
> Built a ride booking platform using FastAPI, PostgreSQL, Redis, WebSockets,
> Celery and Docker. Implemented a validated ride state machine with
> optimistic concurrency control, idempotent ride creation, Redis
> geospatial driver matching with atomic (race-safe) reservation, JWT auth
> with refresh-token rotation and RBAC, the transactional outbox pattern for
> reliable event delivery, real-time trip tracking over WebSockets, rate
> limiting, background jobs, structured logging, health/readiness probes,
> Alembic migrations, and an automated test suite running in CI.

---

## Run it

### Prerequisites
Docker and Docker Compose.

### Steps
```bash
cp .env.example .env
docker compose up --build
```

- API docs (Swagger UI): http://localhost:8000/docs
- Frontend demo client: http://localhost:5173
- Health check: http://localhost:8000/health/ready

The `migrate` service applies all Alembic migrations before `api` starts, so
the database schema is ready on first boot.

### Running the backend locally without Docker
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# point DATABASE_URL / REDIS_URL in .env at local services, then:
alembic upgrade head
uvicorn app.main:app --reload
```

### Running tests
```bash
cd backend
pip install -r requirements.txt
pytest tests/ -v --cov=app --cov-report=term-missing
```
The test suite needs **no external services** — it swaps Postgres for
in-memory SQLite and Redis for `fakeredis` via dependency overrides (see
`backend/tests/conftest.py`), so it's fast and CI-friendly. CI additionally
runs the migrations and the same suite against real Postgres + Redis
containers as a stronger integration check.

### Linting
```bash
cd backend && ruff check app tests
```

---

## API reference

Base URL: `http://localhost:8000`

### Auth
| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/auth/register` | Create a passenger, driver, or admin account |
| POST | `/api/v1/auth/login` | Returns `{access_token, refresh_token}` |
| POST | `/api/v1/auth/refresh` | Rotates the refresh token; reuse of a rotated token is rejected |
| POST | `/api/v1/auth/logout` | Revokes a refresh token |
| GET | `/api/v1/auth/me` | Current user (requires `Authorization: Bearer <access_token>`) |

### Drivers (role: driver)
| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/drivers/me/heartbeat` | `{lat, lng}` — updates location, feeds the Redis GEO index if available |
| POST | `/api/v1/drivers/me/availability` | `{is_available}` — joins/leaves the matching pool |
| GET | `/api/v1/drivers/me` | Current driver profile |

### Rides
| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/rides` | role: passenger. Supports `Idempotency-Key` header |
| GET | `/api/v1/rides` | List your rides (passenger or driver view) |
| GET | `/api/v1/rides/{id}` | Fetch one ride |
| POST | `/api/v1/rides/{id}/transition` | `{target_status, expected_version}` — drives the state machine |

Example — request a ride idempotently:
```bash
curl -X POST http://localhost:8000/api/v1/rides \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Idempotency-Key: 8f14e45f-ceea-4d97-9e88-example" \
  -H "Content-Type: application/json" \
  -d '{"pickup_lat":12.9716,"pickup_lng":77.5946,"dropoff_lat":12.9352,"dropoff_lng":77.6146}'
```

Example — try to match a driver:
```bash
curl -X POST http://localhost:8000/api/v1/rides/$RIDE_ID/transition \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_status":"matching","expected_version":1}'
```

### Real-time
`ws://localhost:8000/ws/rides/{ride_id}?token=<access_token>` — streams JSON
events (`ride.status_changed`, etc.) as they're published to the ride's
Redis pub/sub channel by the outbox worker.

### Health
- `GET /health/live` — process liveness, no dependency checks
- `GET /health/ready` — checks DB + Redis connectivity, returns 503 if either is down

---

## Design notes worth reading

- **Why optimistic concurrency instead of row locks?** Ride transitions are
  infrequent relative to reads, so a conditional `UPDATE ... WHERE id = ? AND
  version = ?` is cheaper than pessimistic locking while still preventing
  lost updates — the caller gets a `409` if they raced another writer, instead
  of one write silently clobbering the other.
- **Why the transactional outbox instead of publishing directly in the
  request handler?** If the ride's status is committed to Postgres but the
  Redis publish fails (network blip, Redis restart), a direct-publish design
  loses the event forever. Writing the event to the same DB transaction as
  the status change, then having a separate poller deliver it, means the
  event is never lost — worst case it's delivered late.
- **Why `SET key value NX EX ttl` instead of a Lua script for driver
  reservation?** `SET ... NX` is already a single atomic Redis command — no
  separate read-then-write, so it's race-safe without the operational
  overhead of a Lua script. A short TTL means a crashed matching attempt
  doesn't strand a driver as permanently "reserved."

## Caveats / what's stubbed

- **Payments** are not implemented at all.
- **Maps / routing / ETAs**: fare estimation uses straight-line (haversine)
  distance, not a real routing engine.
- **SMS / push notifications**: `send_ride_notification` (Celery task) logs
  the event instead of calling a real provider (Twilio, FCM, etc.).
- **Identity verification** (driver license/background checks) is out of
  scope.

Before claiming any of the above as implemented on a resume, wire in real
sandbox credentials for the relevant provider and replace the stub.

## License

MIT — see [LICENSE](LICENSE).
