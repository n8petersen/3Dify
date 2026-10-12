import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _random_session_id() -> str:
    return secrets.token_hex(32)


def _default_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=30)


class Session(SQLModel, table=True):
    __tablename__ = "sessions"

    id: str = Field(default_factory=_random_session_id, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    expires_at: datetime = Field(default_factory=_default_expiry)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
