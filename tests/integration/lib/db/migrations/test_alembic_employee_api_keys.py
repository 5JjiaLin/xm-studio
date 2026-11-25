"""Migration preserves legacy keys but revokes scoped keys before downgrade."""

import sqlalchemy as sa

from alembic import command


def test_upgrade_preserves_legacy_keys_and_downgrade_does_not_expand_employee_keys(alembic_cfg):
    cfg, db_path = alembic_cfg
    parent, revision = "b6a4f10d2c83", "c7d5a20e3b94"
    command.upgrade(cfg, parent)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO api_keys (name, key_hash, key_prefix, created_at, updated_at, user_id) "
                "VALUES ('legacy', 'legacy-hash', 'arc-lega', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'default')"
            ))
        command.upgrade(cfg, revision)
        with engine.begin() as conn:
            assert conn.execute(sa.text(
                "SELECT employee_session_version, project_id, project_permission FROM api_keys WHERE name='legacy'"
            )).one() == (None, None, None)
            conn.execute(sa.text(
                "INSERT INTO api_keys (name, key_hash, key_prefix, created_at, updated_at, user_id, "
                "employee_session_version, project_id, project_permission) VALUES "
                "('employee', 'employee-hash', 'arc-empl', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'default', "
                "1, '19c9bcbb-e9cb-4b71-ae28-199512756fb8', 'read')"
            ))
        command.downgrade(cfg, parent)
        with engine.connect() as conn:
            assert list(conn.scalars(sa.text("SELECT name FROM api_keys"))) == ["legacy"]
        assert "project_id" not in {column["name"] for column in sa.inspect(engine).get_columns("api_keys")}
        command.upgrade(cfg, revision)
        with engine.connect() as conn:
            assert list(conn.scalars(sa.text("SELECT name FROM api_keys"))) == ["legacy"]
    finally:
        engine.dispose()
