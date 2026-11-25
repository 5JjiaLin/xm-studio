"""Migration coverage for company identity foundations."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config

from alembic import command

REVISION = "b6a4f10d2c83"
PARENT = "da7111b6a01f"


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(engine).get_columns(table)}


def test_upgrade_and_downgrade_preserve_shared_user_and_unknown_history(
    alembic_cfg: tuple[Config, Path],
) -> None:
    cfg, db_path = alembic_cfg
    command.upgrade(cfg, PARENT)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        command.upgrade(cfg, REVISION)
        with engine.begin() as conn:
            legacy = conn.execute(sa.text("SELECT id, username, role FROM users WHERE id='default'")).one()
            assert tuple(legacy) == ("default", "admin", "admin")
            assert conn.execute(sa.text("SELECT password_hash FROM users WHERE id='default'")).scalar_one() is None
            conn.execute(
                sa.text(
                    "INSERT INTO project_memberships (project_id, user_id) VALUES ('project-stable-1', 'default')"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO audit_events (actor_label, action, outcome) "
                    "VALUES ('unknown', 'legacy-import', 'unattributed')"
                )
            )
            event = conn.execute(sa.text("SELECT actor_user_id, actor_label FROM audit_events")).one()
            assert tuple(event) == (None, "unknown")
        assert {"password_hash", "session_version", "failed_login_attempts", "locked_until"} <= _columns(
            engine, "users"
        )
    finally:
        engine.dispose()

    command.downgrade(cfg, PARENT)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        assert "password_hash" not in _columns(engine, "users")
        assert "project_memberships" not in sa.inspect(engine).get_table_names()
        assert "audit_events" not in sa.inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT id FROM users WHERE id='default'")).scalar_one() == "default"
    finally:
        engine.dispose()
