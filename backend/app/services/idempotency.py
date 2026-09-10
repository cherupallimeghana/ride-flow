"""
Idempotency-Key handling for unsafe (POST) operations.

Clients supply an `Idempotency-Key` header. We hash the request body
into a fingerprint and store (key, scope, fingerprint) -> response.
- Same key + same fingerprint -> replay the cached response (no new
  side effects, e.g. no duplicate ride).
- Same key + different fingerprint -> reject (409); the client is
  reusing a key for a different logical request, which is a bug on
  their side and must not silently do the wrong thing.
- No key -> operation proceeds normally without idempotency
  protection (caller's choice).
"""
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IdempotencyRecord


def fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def get_cached_response(db: AsyncSession, key: str, scope: str) -> IdempotencyRecord | None:
    result = await db.execute(
        select(IdempotencyRecord).where(IdempotencyRecord.key == key, IdempotencyRecord.scope == scope)
    )
    return result.scalar_one_or_none()


async def store_response(
    db: AsyncSession, key: str, scope: str, request_payload: dict, status_code: int, response_body: dict
) -> None:
    db.add(
        IdempotencyRecord(
            key=key,
            scope=scope,
            request_fingerprint=fingerprint(request_payload),
            status_code=status_code,
            response_body=response_body,
        )
    )
