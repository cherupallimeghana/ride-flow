"""Registration, login, token refresh (with rotation), and logout."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.logging_config import get_logger
from app.models import RefreshToken, User
from app.schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse, UserOut
from app.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
log = get_logger(__name__)


async def _issue_token_pair(db: AsyncSession, user: User) -> TokenResponse:
    access_token = create_access_token(subject=user.id, role=user.role.value)
    raw_refresh, refresh_hash, expires_at = generate_refresh_token()
    db.add(RefreshToken(user_id=user.id, token_hash=refresh_hash, expires_at=expires_at))
    return TokenResponse(access_token=access_token, refresh_token=raw_refresh)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        role=payload.role,
    )
    db.add(user)
    await db.flush()
    log.info("user_registered", user_id=user.id, role=user.role.value)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    log.info("user_login", user_id=user.id)
    return await _issue_token_pair(db, user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """
    Refresh-token rotation: the presented token is immediately revoked
    and a brand-new pair is issued. If a *revoked* token is ever
    presented again, that's a strong signal of token theft/replay —
    in a production system this would also revoke the whole token
    family; here we revoke the specific token and log the anomaly.
    """
    token_hash = hash_refresh_token(payload.refresh_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    stored = result.scalar_one_or_none()

    if stored is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    if stored.revoked:
        log.warning("refresh_token_reuse_detected", user_id=stored.user_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token has been revoked")

    expires_at = stored.expires_at
    if expires_at.tzinfo is None:
        # SQLite (used in tests) doesn't preserve tzinfo on round-trip;
        # Postgres does. Normalize defensively so this comparison is
        # correct regardless of backend.
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")

    stored.revoked = True
    user = await db.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    return await _issue_token_pair(db, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    token_hash = hash_refresh_token(payload.refresh_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    stored = result.scalar_one_or_none()
    if stored is not None:
        stored.revoked = True


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user
