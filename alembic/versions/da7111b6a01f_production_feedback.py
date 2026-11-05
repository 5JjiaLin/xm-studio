"""Local production feedback evidence.

Revision ID: da7111b6a01f
Revises: c6e2a14f093b
"""

from sqlalchemy import inspect

from alembic import op

revision: str = "da7111b6a01f"
down_revision: str | None = "c6e2a14f093b"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    schema = """
CREATE TABLE IF NOT EXISTS production_feedback_settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS production_feedback_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, operation_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS production_feedback_prepared (id TEXT PRIMARY KEY, resource TEXT NOT NULL, before_hash TEXT NOT NULL, after_hash TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_feedback_pending ON production_feedback_prepared(resource,state);
CREATE TABLE IF NOT EXISTS production_feedback_snapshots (digest TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS production_feedback_objects (project TEXT NOT NULL, object_key TEXT NOT NULL, chain_id TEXT NOT NULL, active INTEGER NOT NULL, PRIMARY KEY(project,object_key));
CREATE TABLE IF NOT EXISTS production_feedback_requests (operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, status INTEGER, response TEXT);
"""
    for statement in schema.split(";"):
        if statement.strip():
            connection.exec_driver_sql(statement)


def downgrade():
    connection = op.get_bind()
    names = set(inspect(connection).get_table_names())
    for name in (
        "production_feedback_requests",
        "production_feedback_objects",
        "production_feedback_snapshots",
        "production_feedback_prepared",
        "production_feedback_events",
        "production_feedback_settings",
    ):
        if name in names:
            op.drop_table(name)
