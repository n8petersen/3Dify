import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_session
from models.session import Session
from models.user import User

logger = logging.getLogger("auth")

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,24}$")
_DISPLAY_NAME_RE = re.compile(r"^[^<>]{1,50}$")

# Dummy hash used for constant-time responses when user doesn't exist
_DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt()).decode()


# ── Password hashing ───────────────────────────────────────


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ── Validation ──────────────────────────────────────────────


def validate_username(username: str) -> str:
    username = username.strip()
    if not _USERNAME_RE.match(username):
        raise ValueError(
            "Username must be 3-24 characters: letters, numbers, hyphens, underscores"
        )
    return username


def validate_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    if len(password) > 128:
        raise ValueError("Password must be at most 128 characters")
    return password


def validate_display_name(name: str | None) -> str | None:
    if name is None:
        return None
    name = name.strip()
    if not name:
        return None
    if not _DISPLAY_NAME_RE.match(name):
        raise ValueError("Display name must be 1-50 characters and must not contain < or >")
    return name


# ── Session CRUD ────────────────────────────────────────────


async def create_session(
    db: AsyncSession,
    user_id: str,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Session:
    sess = Session(
        user_id=user_id,
        expires_at=datetime.utcnow() + timedelta(days=settings.session_max_age_days),
        ip_address=ip,
        user_agent=user_agent,
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)
    return sess


async def get_session_user(db: AsyncSession, session_id: str) -> Optional[User]:
    result = await db.execute(
        select(Session).where(
            Session.id == session_id,
            Session.expires_at > datetime.utcnow(),
        )
    )
    sess = result.scalar_one_or_none()
    if not sess:
        return None

    result = await db.execute(select(User).where(User.id == sess.user_id))
    user = result.scalar_one_or_none()
    if not user or user.is_banned:
        return None
    return user


async def delete_session(db: AsyncSession, session_id: str) -> None:
    await db.execute(delete(Session).where(Session.id == session_id))
    await db.commit()


async def delete_user_sessions(db: AsyncSession, user_id: str) -> int:
    """Delete all sessions for a user (e.g. on ban)."""
    result = await db.execute(delete(Session).where(Session.user_id == user_id))
    await db.commit()
    return result.rowcount


async def cleanup_expired_sessions(db: AsyncSession) -> int:
    result = await db.execute(
        delete(Session).where(Session.expires_at <= datetime.utcnow())
    )
    await db.commit()
    return result.rowcount


# ── FastAPI dependencies ────────────────────────────────────


def _get_session_id(request: Request) -> Optional[str]:
    return request.cookies.get(settings.session_cookie_name)


async def get_optional_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Optional[User]:
    session_id = _get_session_id(request)
    if not session_id:
        return None
    return await get_session_user(db, session_id)


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> User:
    user = await get_optional_user(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user
