"""The off-loop transcript boundary must retain the locked SDK's semantics."""

import asyncio
from dataclasses import asdict
from uuid import uuid4

import pytest
from claude_agent_sdk import (
    get_session_messages_from_store as sdk_messages,
)
from claude_agent_sdk import (
    get_subagent_messages_from_store as sdk_subagent,
)
from claude_agent_sdk import (
    list_subagents_from_store as sdk_subagents,
)

from server.agent_runtime.sdk_transcript_adapter import (
    SdkTranscriptAdapter,
    get_session_messages_from_store,
    get_subagent_messages_from_store,
    list_subagents_from_store,
)


class MemoryStore:
    def __init__(self, entries, subpath="subagents/workflows/run-1/agent-child"):
        self.entries = entries
        self.subpath = subpath

    async def load(self, key):
        return self.entries if not key.get("subpath") or key["subpath"] == self.subpath else []

    async def list_subkeys(self, key):
        return [self.subpath, "metadata/unrelated", "subagents/agent-other"]


@pytest.mark.parametrize("subagent", [False, True])
@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"type": "custom-title", "customTitle": "ignored"}, None, {"uuid": 1, "type": "user"}],
        [
            {"type": "user", "uuid": "u1", "parentUuid": None, "message": {"role": "user", "content": "first"}},
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "reply"}],
                },
            },
            {"type": "user", "uuid": "u2", "parentUuid": "a1", "message": {"role": "user", "content": "next"}},
            {"type": "agent_metadata", "toolUseId": "tool-child", "parentAgentId": "parent"},
        ],
        [
            {"type": "user", "uuid": "old", "parentUuid": None, "message": {"role": "user", "content": "old"}},
            {
                "type": "user",
                "uuid": "summary",
                "parentUuid": None,
                "isCompactSummary": True,
                "message": {"role": "user", "content": "summary"},
            },
            {
                "type": "assistant",
                "uuid": "a2",
                "parentUuid": "summary",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "continued"}],
                },
            },
        ],
    ],
)
async def test_store_decoders_match_public_sdk(tmp_path, entries, subagent):
    session_id = str(uuid4())
    store = MemoryStore(entries)
    args = (store, session_id, "child") if subagent else (store, session_id)
    helper = get_subagent_messages_from_store if subagent else get_session_messages_from_store
    original = sdk_subagent if subagent else sdk_messages
    actual = await helper(*args, directory=str(tmp_path))
    # The reference SDK helper executes entirely in a test-owned worker with
    # an in-memory store; no application database is transferred to that loop.
    expected = await asyncio.to_thread(lambda: asyncio.run(original(*args, directory=str(tmp_path))))
    assert [asdict(message) for message in actual] == [asdict(message) for message in expected]


async def test_subagent_listing_matches_public_sdk(tmp_path):
    store = MemoryStore([])
    session_id = str(uuid4())
    actual = await list_subagents_from_store(store, session_id, directory=str(tmp_path))
    expected = await asyncio.to_thread(lambda: asyncio.run(sdk_subagents(store, session_id, directory=str(tmp_path))))
    assert actual == expected == ["child", "other"]


async def test_nested_subagent_retains_raw_metadata_and_store_loop(tmp_path):
    owner_loop = asyncio.get_running_loop()
    subpath = "subagents/workflows/run-1/agent-child"
    main = [
        {
            "type": "user",
            "uuid": "anchor",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-child", "content": "done"},
                ]
            },
            "toolUseResult": {"agentId": "child"},
        }
    ]
    child = [
        {"type": "agent_metadata", "toolUseId": "tool-child", "parentAgentId": "parent"},
        {
            "type": "assistant",
            "uuid": "answer",
            "parentUuid": None,
            "timestamp": "2026-11-10T09:00:00Z",
            "isCompactSummary": True,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "child reply"}]},
        },
    ]

    class Store:
        async def load(self, key):
            assert asyncio.get_running_loop() is owner_loop
            return child if key.get("subpath") == subpath else ([] if key.get("subpath") else main)

        async def list_subkeys(self, key):
            assert asyncio.get_running_loop() is owner_loop
            return [subpath]

    result = await SdkTranscriptAdapter(Store()).read_subagent_timelines(str(uuid4()), tmp_path)
    assert result["tool-child"][0] == {
        "type": "assistant",
        "uuid": "answer",
        "parent_tool_use_id": "tool-child",
        "content": [{"type": "text", "text": "child reply"}],
        "timestamp": "2026-11-10T09:00:00Z",
        "is_compact_summary": True,
    }


@pytest.mark.parametrize("sidecars", ["fresh", "stale", "missing"])
async def test_session_summary_listing_matches_public_sdk(tmp_path, sidecars):
    from claude_agent_sdk import fold_session_summary
    from claude_agent_sdk import list_sessions_from_store as original

    from server.agent_runtime.sdk_transcript_adapter import list_sessions_from_store
    from tests.factories import make_sdk_transcript_entry

    owner_loop = asyncio.get_running_loop()
    sid = str(uuid4())
    entries = [
        make_sdk_transcript_entry("u1", None, "user", sid, "first request"),
        make_sdk_transcript_entry("a1", "u1", "assistant", sid, "reply"),
        make_sdk_transcript_entry("u2", "a1", "user", sid, "last request"),
        {"type": "custom-title", "customTitle": "preferred title"},
    ]

    class SummaryStore:
        async def list_sessions(self, project_key):
            return [{"session_id": sid, "mtime": 10}]

        async def list_session_summaries(self, project_key):
            if sidecars == "missing":
                return []
            summary = fold_session_summary(None, {"project_key": project_key, "session_id": sid}, entries)
            return [{**summary, "mtime": 10 if sidecars == "fresh" else 1}]

        async def load(self, key):
            return entries

    class LoopBoundStore(SummaryStore):
        async def load(self, key):
            assert asyncio.get_running_loop() is owner_loop
            return await super().load(key)

    actual = await list_sessions_from_store(LoopBoundStore(), directory=str(tmp_path))
    expected = await asyncio.to_thread(lambda: asyncio.run(original(SummaryStore(), directory=str(tmp_path))))
    assert [asdict(info) for info in actual] == [asdict(info) for info in expected]
    assert actual[0].summary == "preferred title"
