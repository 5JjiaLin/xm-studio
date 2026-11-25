"""Project membership authorization and audit behavior."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from lib.db.models import AuditEvent
from lib.identity.accounts import create_employee
from lib.identity.projects import (
    ProjectAccessDenied,
    list_project_members,
    remove_project_member,
    require_project_access,
    set_project_member,
)


async def _employee(session, username: str, role: str):
    return await create_employee(session, username=username, password="long-test-password", role=role)


async def test_project_membership_enforces_role_and_permission_actions(db_session):
    project_id = str(uuid4())
    admin = await _employee(db_session, "project-admin", "admin")
    editor = await _employee(db_session, "project-editor", "editor")
    viewer = await _employee(db_session, "project-viewer", "viewer")
    await set_project_member(
        db_session, actor_id=admin.id, project_id=project_id, user_id=editor.id, permission="editor"
    )
    await set_project_member(
        db_session, actor_id=admin.id, project_id=project_id, user_id=viewer.id, permission="viewer"
    )

    for action in ("read", "write", "download"):
        await require_project_access(db_session, user_id=admin.id, project_id=project_id, action=action)
        await require_project_access(db_session, user_id=editor.id, project_id=project_id, action=action)
    await require_project_access(db_session, user_id=viewer.id, project_id=project_id, action="read")

    for user_id, action in ((viewer.id, "write"), (viewer.id, "download"), (editor.id, "manage")):
        with pytest.raises(ProjectAccessDenied):
            await require_project_access(db_session, user_id=user_id, project_id=project_id, action=action)


async def test_revocation_and_account_role_change_take_effect_immediately(db_session):
    project_id = str(uuid4())
    admin = await _employee(db_session, "revoke-admin", "admin")
    editor = await _employee(db_session, "revoke-editor", "editor")
    await set_project_member(
        db_session, actor_id=admin.id, project_id=project_id, user_id=editor.id, permission="editor"
    )
    await require_project_access(db_session, user_id=editor.id, project_id=project_id, action="write")

    assert await remove_project_member(db_session, actor_id=admin.id, project_id=project_id, user_id=editor.id)
    with pytest.raises(ProjectAccessDenied):
        await require_project_access(db_session, user_id=editor.id, project_id=project_id, action="read")

    await set_project_member(
        db_session, actor_id=admin.id, project_id=project_id, user_id=editor.id, permission="viewer"
    )
    await db_session.refresh(editor)
    editor.role = "viewer"
    await db_session.flush()
    await require_project_access(db_session, user_id=editor.id, project_id=project_id, action="read")
    with pytest.raises(ProjectAccessDenied):
        await require_project_access(db_session, user_id=editor.id, project_id=project_id, action="write")


async def test_member_changes_are_audited_and_list_is_admin_only(db_session):
    project_id = str(uuid4())
    admin = await _employee(db_session, "audit-admin", "admin")
    editor = await _employee(db_session, "audit-editor", "editor")
    await set_project_member(
        db_session, actor_id=admin.id, project_id=project_id, user_id=editor.id, permission="editor"
    )
    assert await list_project_members(db_session, actor_id=admin.id, project_id=project_id) == [
        {
            "id": editor.id,
            "username": editor.username,
            "role": "editor",
            "is_active": True,
            "permission": "editor",
        }
    ]
    with pytest.raises(ProjectAccessDenied):
        await list_project_members(db_session, actor_id=editor.id, project_id=project_id)

    await remove_project_member(db_session, actor_id=admin.id, project_id=project_id, user_id=editor.id)
    events = list(
        (
            await db_session.scalars(
                select(AuditEvent).where(AuditEvent.project_id == project_id).order_by(AuditEvent.id)
            )
        ).all()
    )
    assert [(event.actor_user_id, event.action, event.object_id) for event in events] == [
        (admin.id, "project.member.set", editor.id),
        (admin.id, "project.member.remove", editor.id),
    ]


@pytest.mark.parametrize("project_id", ["not-a-uuid", ""])
async def test_invalid_project_identity_fails_closed(db_session, project_id):
    admin = await _employee(db_session, "invalid-id-admin", "admin")
    with pytest.raises(ValueError, match="canonical UUID"):
        await require_project_access(db_session, user_id=admin.id, project_id=project_id, action="read")
