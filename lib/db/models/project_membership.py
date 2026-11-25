"""Project membership keyed by a stable project identifier."""

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, TimestampMixin


class ProjectMembership(TimestampMixin, Base):
    __tablename__ = "project_memberships"

    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    permission: Mapped[str] = mapped_column(String, nullable=False, server_default="editor")
