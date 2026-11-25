"""Real employee authentication, session storage and concurrent assistant admission.

Only the external SDK process/provider credentials are replaced. HTTP routes,
project authorization, options assembly, actor, metadata and event writes are real.
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from lib.db.models.session_event import AgentSessionEventLogEntry
from lib.identity.accounts import create_employee
from lib.identity.projects import set_project_member
from server import project_access
from server.agent_runtime.event_log import build_user_entry
from server.agent_runtime.service import AssistantService
from server.auth import create_employee_token
from server.error_handlers import register_error_handlers
from server.project_access import require_project_request_access
from server.routers import assistant
from tests.fakes import (
    FakeSDKClient,
    bind_session_factory,
    command_lifecycle_frame,
    result_frame,
    system_frame,
    use_provider_env,
    use_sdk_client,
)


@pytest.fixture
async def employees(tmp_path, concurrent_session_factory, monkeypatch):
    factory = concurrent_session_factory
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    bind_session_factory(monkeypatch, factory)
    service = AssistantService(project_root=tmp_path / "repo", data_root=tmp_path / "data", session_factory=factory)
    for name in ("alpha", "beta"):
        service.pm.create_project(name)
    monkeypatch.setattr(project_access, "get_project_manager", lambda: service.pm)
    monkeypatch.setattr(assistant, "get_assistant_service", lambda: service)
    async with factory() as session, session.begin():
        admin = await create_employee(session, username="isolation-admin", password="long-test-password", role="admin")
        alice = await create_employee(session, username="isolation-alice", password="long-test-password", role="editor")
        bob = await create_employee(session, username="isolation-bob", password="long-test-password", role="editor")
        for name in ("alpha", "beta"):
            project_id = service.pm.load_project(name)["project_id"]
            for employee in (alice, bob):
                await set_project_member(
                    session, actor_id=admin.id, project_id=project_id, user_id=employee.id, permission="editor"
                )
    tokens = {
        user.id: {"Authorization": "Bearer " + create_employee_token(user.id, user.username, user.session_version)}
        for user in (alice, bob, admin)
    }
    app = FastAPI()
    app.include_router(
        assistant.router,
        prefix="/api/v1/projects/{project_name}/assistant",
        dependencies=[Depends(require_project_request_access)],
    )
    register_error_handlers(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        try:
            yield service, client, factory, (alice, bob, admin), tokens
        finally:
            await service.shutdown()


async def test_same_project_sessions_are_private_on_every_active_route(employees):
    service, client, _factory, (alice, bob, admin), tokens = employees
    alice_id = str(uuid4())
    await service.meta_store.create("alpha", alice_id, user_id=alice.id)
    await service.event_log_store.for_user(alice.id).append(
        alice_id, [build_user_entry([{"type": "text", "text": "alice private"}])]
    )
    bob_ids = [str(uuid4()), str(uuid4())]
    for sid in bob_ids:
        await service.meta_store.create("alpha", sid, user_id=bob.id)
    legacy_id = str(uuid4())
    await service.meta_store.create("alpha", legacy_id)
    base = "/api/v1/projects/alpha/assistant/sessions"
    actions = [
        ("GET", f"/{alice_id}", None),
        ("DELETE", f"/{alice_id}", None),
        ("GET", f"/{alice_id}/entries", None),
        ("GET", f"/{alice_id}/entries/stream", None),
        ("POST", "/send", {"session_id": alice_id, "content": "intrusion"}),
        ("POST", f"/{alice_id}/rewrite", {"anchor_entry_uuid": "anchor", "content": "intrusion"}),
        ("POST", f"/{alice_id}/interrupt", None),
        ("POST", f"/{alice_id}/questions/q1/answer", {"answers": {"q": "intrusion"}}),
        ("POST", f"/{alice_id}/queued-messages/q1/edit", None),
        ("DELETE", f"/{alice_id}/queued-messages/q1", None),
        ("POST", f"/{alice_id}/queued-messages/q1/resend", None),
        ("POST", f"/{alice_id}/queued-messages/q1/send-now", None),
    ]
    for employee in (bob, admin):
        for method, path, body in actions:
            response = await client.request(method, base + path, headers=tokens[employee.id], json=body)
            assert response.status_code == 404, (employee.username, method, path, response.text)
            assert "alice private" not in response.text
    # The account owner can read history, while old shared sessions stay private.
    response = await client.get(f"{base}/{alice_id}/entries", headers=tokens[alice.id])
    assert response.status_code == 200
    assert "alice private" in response.text
    assert (await client.get(f"{base}/{legacy_id}", headers=tokens[alice.id])).status_code == 404
    pages = []
    for offset in (0, 1):
        page = await client.get(base, params={"limit": 1, "offset": offset}, headers=tokens[bob.id])
        assert page.status_code == 200
        pages.extend(entry["id"] for entry in page.json()["sessions"])
    assert set(pages) == set(bob_ids)  # Filter before LIMIT/OFFSET.
    assert await service.meta_store.get(alice_id) is not None
    assert service.session_manager.sessions == {}  # Denied mutations never started an Agent.


async def test_concurrent_employee_admission_retry_and_resume_keep_owner(employees, monkeypatch):
    service, client, factory, (alice, bob, _admin), tokens = employees
    captured = []

    async def provider_env():
        await asyncio.sleep(0)  # Deliberately interleave option construction.
        return {}

    def sdk_client(options):
        owner = options.session_store._user_id
        sid = options.resume or options.session_id or str(uuid4())
        captured.append((owner, sid, options))
        return FakeSDKClient(
            frames=[
                system_frame("init", session_id=sid),
                lambda sdk: command_lifecycle_frame(sdk.sent_messages[-1]["uuid"], "started", session_id=sid),
                result_frame(session_id=sid),
                system_frame("session_state_changed", session_id=sid, state="idle"),
            ]
        )

    use_provider_env(monkeypatch, provider_env)
    use_sdk_client(monkeypatch, sdk_client)
    for employee in (alice, bob):
        memory = service.layout.user_memory_dir(employee.id)
        memory.mkdir(parents=True)
        (memory / "MEMORY.md").write_text(f"private-memory-{employee.username}", encoding="utf-8")
    base = "/api/v1/projects/alpha/assistant/sessions"

    async def send(user, project="alpha", **body):
        response = await client.post(
            f"/api/v1/projects/{project}/assistant/sessions/send",
            headers=tokens[user.id],
            json={"content": "hello", **body},
        )
        assert response.status_code == 200, response.text
        return response.json()

    first = await asyncio.gather(*(send(user, client_key="same-key") for user in (alice, bob)))
    assert first[0]["session_id"] != first[1]["session_id"]
    for user, result in zip((alice, bob), first, strict=True):
        sid = result["session_id"]
        assert (await service.meta_store.get(sid)).user_id == user.id
        assert service.session_manager.sessions[sid].user_id == user.id
        assert (await send(user, client_key="same-key"))["session_id"] == sid
    assert len(captured) == 2
    # Cold recovery uses persisted owner/project, even with identical keys.
    service._new_session_client_keys.clear()
    for user, result in zip((alice, bob), first, strict=True):
        assert (await send(user, client_key="same-key"))["session_id"] == result["session_id"]
    beta = await send(alice, project="beta", client_key="same-key")
    service._new_session_client_keys.clear()
    assert beta["session_id"] != first[0]["session_id"]
    assert (await send(alice, client_key="same-key"))["session_id"] == first[0]["session_id"]
    assert len(captured) == 3
    # Each options object and its long-lived hook retains its own owner.
    for owner, _sid, options in captured:
        other = bob if owner == alice.id else alice
        assert f"private-memory-{other.username}" not in options.system_prompt["append"]
        hook = options.hooks["PreToolUse"][0].hooks[-1]
        for target, allowed in ((owner, True), (other.id, False)):
            decision = await hook(
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": str(service.layout.user_memory_dir(target) / "MEMORY.md")},
                },
                None,
                None,
            )
            assert (decision.get("continue_") is True) == allowed

    async def wait_settled(sid):
        async with asyncio.timeout(5):
            async with service.session_manager.stream_messages(sid, idle_timeout=0.1) as stream:
                await anext(stream)  # Subscribe before checking the live state.
                if service.session_manager.sessions[sid].status != "running":
                    return
                async for _event in stream:
                    if service.session_manager.sessions[sid].status != "running":
                        return

    alice_sid = first[0]["session_id"]
    await asyncio.gather(*(wait_settled(sid) for sid in service.session_manager.sessions))
    await service.session_manager.close_session(alice_sid)
    await send(alice, session_id=alice_sid, client_key="resume-key")
    assert captured[-1][0] == alice.id
    assert captured[-1][2].resume == alice_sid
    await wait_settled(alice_sid)
    async with factory() as session:
        rows = (await session.execute(select(AgentSessionEventLogEntry))).scalars().all()
    expected = {first[0]["session_id"]: alice.id, first[1]["session_id"]: bob.id, beta["session_id"]: alice.id}
    assert rows
    assert all(row.user_id == expected[row.session_id] for row in rows)
    assert (await client.get(f"{base}/{alice_sid}", headers=tokens[bob.id])).status_code == 404


async def test_owner_scoped_transcript_reads_deletes_and_forks(employees):
    from lib.agent.agent_session_store import make_project_key
    from server.agent_runtime.sdk_transcript_adapter import delete_session_via_store
    from tests.factories import make_sdk_transcript_entry

    service, _client, _factory, (alice, bob, _admin), _tokens = employees
    sid = str(uuid4())
    meta = await service.meta_store.create("alpha", sid, user_id=alice.id)
    cwd = service.layout.projects_dir / "alpha"
    project_key = make_project_key(cwd)
    key = {"project_key": project_key, "session_id": sid}
    own = service.session_manager._build_session_store(user_id=alice.id)
    other = service.session_manager._build_session_store(user_id=bob.id)
    entries = [
        make_sdk_transcript_entry("u1", None, "user", sid, "first request"),
        make_sdk_transcript_entry("a1", "u1", "assistant", sid, "reply"),
        make_sdk_transcript_entry("u2", "a1", "user", sid, "second request"),
    ]
    await own.append(key, entries)
    await own.append({**key, "subpath": "subagents/agent-one"}, entries[:1])
    assert await other.load(key) is None
    assert await other.list_sessions(project_key) == []
    assert await other.list_session_summaries(project_key) == []
    assert await other.list_subkeys(key) == []
    await other.delete(key)
    assert await own.load(key) == entries
    with pytest.raises(PermissionError):
        await other.append(key, entries)
    log_store = service.event_log_store.for_user(alice.id)
    first = build_user_entry([{"type": "text", "text": "first request"}])
    second = build_user_entry([{"type": "text", "text": "second request"}])
    await log_store.append(sid, [first, second])
    await log_store.record_user_message_link(sid, second["uuid"], "u2")
    branch = await service._branch_service_for(meta.user_id).branch(sid, second["uuid"])
    new_meta = await service.meta_store.get(branch.session_id)
    assert new_meta.user_id == alice.id
    assert new_meta.fork_parent_session_id == sid
    branch_key = {**key, "session_id": branch.session_id}
    assert len(await own.load(branch_key)) == 2
    assert await other.load(branch_key) is None
    history = await service.list_session_entries(branch.session_id, meta=new_meta)
    assert any("first request" in str(entry) for entry in history["entries"])
    assert "second request" not in str(history["entries"])
    await delete_session_via_store(own, branch.session_id, directory=str(cwd))
    assert await own.load(branch_key) is None
    assert await own.load(key) == entries
