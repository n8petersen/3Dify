import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_session
from models.user import User
from services import auth as auth_service

logger = logging.getLogger("auth.routes")

router = APIRouter(prefix="/api/auth")

# ── Login rate limiter (in-memory) ──────────────────────────

_login_attempts: dict[str, list[float]] = defaultdict(list)
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_S = 300  # 5 minutes

_register_attempts: dict[str, list[float]] = defaultdict(list)
_MAX_REGISTER_ATTEMPTS = 3
_REGISTER_WINDOW_S = 600  # 10 minutes


def _check_rate(store: dict, ip: str, max_attempts: int, window: int, label: str) -> None:
    now = time.monotonic()
    attempts = store[ip]
    store[ip] = [t for t in attempts if now - t < window]
    if len(store[ip]) >= max_attempts:
        raise HTTPException(429, f"Too many {label} attempts. Try again later.")
    store[ip].append(now)


# ── Cookie helpers ──────────────────────────────────────────

# Secure flag: True unless public_url is http://localhost
_SECURE = not any(
    o.startswith("http://localhost") or o.startswith("http://127.0.0.1")
    for o in settings.cors_origins
)


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        max_age=settings.session_max_age_days * 86400,
        httponly=True,
        secure=_SECURE,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=_SECURE,
        samesite="lax",
        path="/",
    )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


# ── Schemas ─────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


# ── Endpoints ───────────────────────────────────────────────


@router.post("/register")
async def register(
    body: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    ip = _client_ip(request)
    _check_rate(_register_attempts, ip, _MAX_REGISTER_ATTEMPTS, _REGISTER_WINDOW_S, "registration")

    # Validate inputs
    try:
        username = auth_service.validate_username(body.username)
        password = auth_service.validate_password(body.password)
        display_name = auth_service.validate_display_name(body.display_name)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Create user — let the DB unique constraint handle races
    user = User(
        username=username,
        password_hash=auth_service.hash_password(password),
        display_name=display_name or username,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Username already taken")
    await db.refresh(user)
    logger.info("New user registered: %s (%s)", user.username, user.id)

    # Auto-login: create session
    sess = await auth_service.create_session(
        db,
        user_id=user.id,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )

    response = Response(status_code=201)
    response.headers["content-type"] = "application/json"
    _set_session_cookie(response, sess.id)
    response.body = json.dumps({
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
    }).encode()
    return response


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    ip = _client_ip(request)
    _check_rate(_login_attempts, ip, _MAX_LOGIN_ATTEMPTS, _LOGIN_WINDOW_S, "login")

    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()

    if not user:
        # Constant-time: always run bcrypt even when user doesn't exist
        auth_service.verify_password(body.password, auth_service._DUMMY_HASH)
        raise HTTPException(401, "Invalid username or password")

    if not auth_service.verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid username or password")

    # Return same generic error for banned users (don't leak ban status)
    if user.is_banned:
        raise HTTPException(401, "Invalid username or password")

    # Update last login
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    sess = await auth_service.create_session(
        db,
        user_id=user.id,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )

    response = Response(status_code=200)
    response.headers["content-type"] = "application/json"
    _set_session_cookie(response, sess.id)
    response.body = json.dumps({
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
    }).encode()
    return response


@router.get("/me")
async def me(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    user = await auth_service.get_optional_user(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "created_at": user.created_at.isoformat(),
    }


@router.post("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        await auth_service.delete_session(db, session_id)

    response = Response(status_code=200)
    response.headers["content-type"] = "application/json"
    _clear_session_cookie(response)
    response.body = json.dumps({"ok": True}).encode()
    return response
