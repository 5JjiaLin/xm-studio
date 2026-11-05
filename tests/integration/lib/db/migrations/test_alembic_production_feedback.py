"""Feedback schema migration preserves unrelated business records."""
import sqlalchemy as sa

from alembic import command


def test_feedback_upgrade_downgrade(alembic_cfg, migration_revisions):
    revision, parent = migration_revisions("*_production_feedback.py")
    config, database = alembic_cfg
    command.upgrade(config, parent)
    engine = sa.create_engine(f"sqlite:///{database}")
    command.upgrade(config, revision)
    with engine.begin() as connection:
        names = set(sa.inspect(connection).get_table_names())
        assert {"production_feedback_events", "production_feedback_prepared", "production_feedback_settings"} <= names
        assert "api_calls" in names
    command.downgrade(config, parent)
    with engine.begin() as connection:
        names = set(sa.inspect(connection).get_table_names())
        assert "production_feedback_events" not in names
        assert "api_calls" in names
    engine.dispose()
