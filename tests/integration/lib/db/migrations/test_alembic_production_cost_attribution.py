"""Episode snapshots are nullable and migrations retain historical calls."""

import sqlalchemy as sa

from alembic import command


def test_production_attribution_upgrade_and_downgrade(alembic_cfg, migration_revisions):
    revision, parent = migration_revisions("*_add_production_cost_attribution.py")
    config, database = alembic_cfg
    command.upgrade(config, parent)
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text("""INSERT INTO api_calls
                (id,project_name,call_type,model,status,started_at,created_at,updated_at,cost_amount)
                VALUES (1,'old','image','m','success',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,1.25)""")
            )
        command.upgrade(config, revision)
        with engine.begin() as conn:
            assert conn.execute(sa.text("SELECT episode_id,generation_target,cost_amount FROM api_calls")).one() == (
                None,
                None,
                1.25,
            )
        command.downgrade(config, parent)
        with engine.begin() as conn:
            assert conn.execute(sa.text("SELECT cost_amount FROM api_calls")).scalar_one() == 1.25
    finally:
        engine.dispose()
