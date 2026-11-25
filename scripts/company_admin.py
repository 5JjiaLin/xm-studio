"""Interactive local employee-admin initialization/recovery for a stopped deployment.

No HTTP recovery, default password, environment password or command-line password.
This tool never migrates a database, creates a missing database, or starts services.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import os
import subprocess
import sys
import warnings
from contextlib import ExitStack, contextmanager
from pathlib import Path

import portalocker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def validate_root(value: Path) -> Path:
    """Refuse redirected roots, components, DB sidecars, or lock files."""
    if not value.is_absolute():
        raise ValueError("Deployment root must be an absolute path")
    if os.name == "nt":
        import ctypes

        if ctypes.windll.kernel32.GetDriveTypeW(value.anchor) != 3:
            raise ValueError("The active SQLite deployment must be on a local fixed drive")
    for path in (
        value,
        *value.parents,
        value / "data",
        value / "ops",
        value / "data" / "arcreel.db",
        *(value / "data" / f"arcreel.db{suffix}" for suffix in ("-wal", "-shm", "-journal")),
        *(value / "ops" / name for name in ("operations.lock", "instance.lock", "inspection-only.json")),
    ):
        if path.is_symlink() or (os.name == "nt" and path.is_junction()):
            raise ValueError("Deployment paths must not contain symlinks or junctions")
    root = value.resolve(strict=True)
    if not (root / "data").is_dir() or not (root / "ops").is_dir():
        raise ValueError("Existing deployment data and ops directories are required")
    if not (root / "data" / "arcreel.db").is_file():
        raise ValueError("Existing data/arcreel.db is required; no database will be created")
    if (root / "ops" / "inspection-only.json").exists():
        raise ValueError("An inspection-only restore cannot be used for production account changes")
    return root


@contextmanager
def offline_deployment(root: Path):
    """Use the same lock order as company_ops, blocking unattended host startup."""
    with ExitStack() as stack:
        for name in ("operations.lock", "instance.lock"):
            try:
                stack.enter_context(portalocker.Lock(root / "ops" / name, mode="a", timeout=0))
            except portalocker.exceptions.LockException as exc:
                raise RuntimeError(
                    "Deployment is running or another operation is active; stop normally and retry"
                ) from exc
        # Check again with both locks held, before any database access.
        validate_root(root)
        yield


def local_operator() -> str:
    if os.name == "nt":
        executable = Path(os.environ["SYSTEMROOT"]) / "System32" / "whoami.exe"
        output = subprocess.check_output(
            [str(executable), "/user", "/fo", "csv", "/nh"], text=True, creationflags=subprocess.CREATE_NO_WINDOW
        )
        name, sid = next(csv.reader(output.splitlines()))
        return f"{name} ({sid})"
    import pwd

    return f"{pwd.getpwuid(os.getuid()).pw_name} (uid={os.getuid()})"


def prompt_password() -> str:
    # Refuse getpass's echoing fallback, including piped/noninteractive input.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        first = getpass.getpass("New employee administrator password (12+ characters): ")
        second = getpass.getpass("Confirm password: ")
    if len(first) < 12:
        raise ValueError("Password must contain at least 12 characters")
    if first != second:
        raise ValueError("Passwords do not match")
    return first


async def apply_change(
    database: Path, *, action: str, username: str, password: str, operator: str, reason: str
) -> dict:
    """Open only the explicit existing database; serialize account and audit writes."""
    from sqlalchemy import URL, inspect, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    if not database.is_absolute():
        raise ValueError("Database path must be absolute")
    url = URL.create("sqlite+aiosqlite", database=database.as_uri(), query={"mode": "rw", "uri": "true"})
    # lib.db imports a global engine. Pin that import to this explicit existing
    # target so inherited DATABASE_URL or developer data cannot redirect it.
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    from lib.db.models import AuditEvent, User
    from lib.identity.admin_recovery import change_local_admin

    engine = create_async_engine(url, connect_args={"timeout": 0}, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(text("PRAGMA foreign_keys=ON"))
            await session.execute(text("BEGIN IMMEDIATE"))
            connection = await session.connection()

            def check_schema(sync_connection):
                inspector = inspect(sync_connection)
                for model in (User, AuditEvent):
                    if not inspector.has_table(model.__tablename__):
                        raise ValueError(
                            "Employee schema is not ready; migrate the release before local account changes"
                        )
                    columns = {column["name"] for column in inspector.get_columns(model.__tablename__)}
                    if not set(model.__table__.columns.keys()) <= columns:
                        raise ValueError("Employee schema is outdated; this tool does not run migrations")

            await connection.run_sync(check_schema)
            if (await session.execute(text("PRAGMA quick_check"))).scalars().all() != ["ok"]:
                raise ValueError("SQLite integrity check failed; restore a verified backup before account changes")
            employee = await change_local_admin(
                session, action=action, username=username, password=password, operator=operator, reason=reason
            )
            return {"id": employee.id, "username": employee.username, "session_version": employee.session_version}
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("initialize", "recover"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--username", required=True, help="Dedicated employee admin; never the shared account")
    args = parser.parse_args(argv)
    try:
        root = validate_root(args.root)
        if not sys.stdin.isatty():
            raise ValueError("An interactive local terminal is required; password input cannot be piped")
        with offline_deployment(root):
            operator = local_operator()
            print(
                f"Database: {root / 'data' / 'arcreel.db'}\nAction: {args.action}; employee: {args.username}\nOperator: {operator}"
            )
            print("Use a verified pre-change backup. Services remain stopped after this command.")
            reason = input("Ticket/reference or reason (no passwords): ").strip()
            if input(f"Type {args.action} {args.username} to apply: ") != f"{args.action} {args.username}":
                raise ValueError("Confirmation did not match; no account change was made")
            password = prompt_password()
            try:
                result = asyncio.run(
                    apply_change(
                        root / "data" / "arcreel.db",
                        action=args.action,
                        username=args.username,
                        password=password,
                        operator=operator,
                        reason=reason,
                    )
                )
            finally:
                password = ""
        print(
            f"Administrator {result['username']} updated; session version {result['session_version']}. Audit committed."
        )
        return 0
    except (Exception, KeyboardInterrupt):
        # Database-driver exceptions can embed SQL parameters and password hashes.
        # Keep the CLI failure text constant; never dump exception/traceback data.
        print(
            "Command failed. Verify the local audit before retrying; check the stopped deployment, schema, input and permissions.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
