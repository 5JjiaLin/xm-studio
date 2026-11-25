"""User model for multi-user infrastructure."""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, server_default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=sa.true())
    password_hash: Mapped[str | None] = mapped_column(String)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
