"""SDK transcript reads with synchronous work isolated from the event loop.

The SDK's public async store helpers also normalize filesystem paths and
decode conversation chains synchronously. The local compatibility helpers
keep async database reads on their original loop and offload the SDK's pure
decoders. Tests compare this boundary with the locked SDK's public helpers;
SDK upgrades must run those tests. Filesystem rollback still uses the public
``get_session_messages`` reader. Raw payload fields retain timestamps and
structured tool results omitted by SDK SessionMessage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

from claude_agent_sdk import SDKSessionInfo, get_session_messages
from claude_agent_sdk._internal.session_summary import summary_entry_to_sdk_info
from claude_agent_sdk._internal.sessions import (
    _canonicalize_path,
    _entries_to_jsonl,
    _entries_to_session_messages,
    _entries_to_subagent_messages,
    _filter_transcript_entries,
    _jsonl_to_lite,
    _parent_ids_from_agent_metadata,
    _parse_session_info_from_lite,
    _split_agent_metadata,
)

SDK_AVAILABLE = True


async def _store_key(session_id: str, directory: Path | str | None) -> dict[str, str] | None:
    try:
        UUID(session_id)
    except (ValueError, TypeError, AttributeError):
        return None
    from lib.agent.agent_session_store import make_project_key

    return {"project_key": await asyncio.to_thread(make_project_key, directory or "."), "session_id": session_id}


def _decode_main(entries: list[Any]) -> list[Any]:
    return _entries_to_session_messages(_filter_transcript_entries(entries), None, 0)


def _decode_subagent(entries: list[Any]) -> list[Any]:
    meta, transcript = _split_agent_metadata(entries)
    parent_tool_use_id, parent_agent_id = _parent_ids_from_agent_metadata(meta)
    return _entries_to_subagent_messages(
        _filter_transcript_entries(transcript), None, 0, parent_tool_use_id, parent_agent_id
    )


async def get_session_messages_from_store(store: Any, session_id: str, directory: str | None = None) -> list[Any]:
    key = await _store_key(session_id, directory)
    if key is None:
        return []
    return await asyncio.to_thread(_decode_main, await store.load(key) or [])


async def list_subagents_from_store(store: Any, session_id: str, directory: str | None = None) -> list[str]:
    key = await _store_key(session_id, directory)
    if key is None:
        return []
    subpaths = await store.list_subkeys(key)
    return list(
        dict.fromkeys(
            subpath.rsplit("/", 1)[-1][len("agent-") :]
            for subpath in subpaths
            if subpath.startswith("subagents/") and subpath.rsplit("/", 1)[-1].startswith("agent-")
        )
    )


async def get_subagent_messages_from_store(
    store: Any, session_id: str, agent_id: str, directory: str | None = None
) -> list[Any]:
    key = await _store_key(session_id, directory)
    if key is None or not agent_id:
        return []
    subpath = f"subagents/agent-{agent_id}"
    if callable(getattr(store, "list_subkeys", None)):
        subpaths = await store.list_subkeys(key)
        subpath = next(
            (
                path
                for path in subpaths
                if path.startswith("subagents/") and path.rsplit("/", 1)[-1] == f"agent-{agent_id}"
            ),
            "",
        )
        if not subpath:
            return []
    return await asyncio.to_thread(_decode_subagent, await store.load({**key, "subpath": subpath}) or [])


async def list_sessions_from_store(store: Any, directory: str | None = None) -> list[SDKSessionInfo]:
    """List SDK summaries with filesystem work and decoding off-loop.

    Authorized metadata is paginated separately. Fresh sidecars avoid transcript
    reads; missing/stale ones use the locked SDK's JSONL lite decoder, just
    like its public listing helper (including its file-size semantics).
    """
    from lib.agent.agent_session_store import make_project_key

    project_path = await asyncio.to_thread(_canonicalize_path, directory or ".")
    project_key = await asyncio.to_thread(make_project_key, directory or ".")
    listing = await store.list_sessions(project_key)
    summaries = {}
    if callable(getattr(store, "list_session_summaries", None)):
        with suppress(NotImplementedError):
            summaries = {entry["session_id"]: entry for entry in await store.list_session_summaries(project_key)}
    limiter = asyncio.Semaphore(16)

    def decode(entries, sid, mtime):
        parsed = _parse_session_info_from_lite(sid, _jsonl_to_lite(_entries_to_jsonl(entries), mtime), project_path)
        if parsed is not None:
            parsed.last_modified = mtime
        return parsed

    async def info(entry):
        sid, mtime = entry["session_id"], entry["mtime"]
        summary = summaries.get(sid)
        if summary is None or summary["mtime"] < mtime:
            async with limiter:
                try:
                    key = {"project_key": project_key, "session_id": sid}
                    entries = await store.load(key)
                    if not entries:
                        return None
                    return await asyncio.to_thread(decode, entries, sid, mtime)
                except Exception:
                    return SDKSessionInfo(session_id=sid, summary="", last_modified=mtime)
        return await asyncio.to_thread(summary_entry_to_sdk_info, summary, project_path)

    results = await asyncio.gather(*(info(entry) for entry in listing))
    return sorted(
        (result for result in results if result is not None), key=lambda item: item.last_modified, reverse=True
    )


async def delete_session_via_store(store: Any, session_id: str, directory: str | None = None) -> None:
    key = await _store_key(session_id, directory)
    if key is None:
        raise ValueError("invalid session ID")
    await store.delete(key)


class SdkTranscriptAdapter:
    """Read SDK conversation transcripts.

    Constructed with an optional store. When the store is present, reads go
    through the SDK's SessionStore helpers; otherwise they fall back to the
    SDK's filesystem reader (``get_session_messages``) so the rollback path
    (``ARCREEL_SDK_SESSION_STORE=off``) still works.

    ``project_cwd`` is supplied per call because a single AssistantService
    instance serves many projects.
    """

    def __init__(self, store: Any = None) -> None:
        self._store = store

    async def read_raw_messages(
        self,
        sdk_session_id: str | None,
        project_cwd: Path | str | None = None,
    ) -> list[dict[str, Any]]:
        """Read raw messages from the SDK transcript."""
        if not sdk_session_id or not SDK_AVAILABLE:
            return []
        if self._store is not None:
            return await self._read_via_store(sdk_session_id, project_cwd)
        return await self._read_via_legacy(sdk_session_id)

    async def _read_via_store(
        self,
        sdk_session_id: str,
        project_cwd: Path | str | None,
    ) -> list[dict[str, Any]]:
        try:
            messages = await get_session_messages_from_store(
                self._store, sdk_session_id, directory=self._coerce_cwd(project_cwd)
            )
        except Exception:
            logger.warning(
                "Failed to read SDK session %s via store",
                sdk_session_id,
                exc_info=True,
            )
            return []

        # SDK 0.1.71 SessionMessage has no timestamp field — backfill from the
        # store payload we wrote in append() (preserves SDK's payload.timestamp
        # verbatim). This keeps optimistic-turn dedup stable across rounds.
        # toolUseResult（CLI 的结构化工具结果，如 AskUserQuestion 的答案）
        # 同样只存在于 payload，一并按 uuid 回填。
        payload_by_uuid = await self._load_payload_index_from_store(sdk_session_id, project_cwd)
        return [self._adapt(msg, payload_by_uuid) for msg in (messages or [])]

    async def _load_store_entries(
        self,
        sdk_session_id: str,
        project_cwd: Path | str | None,
        subpath: str = "",
    ) -> list[dict[str, Any]]:
        """Read transcript rows while keeping SDK path normalization off-loop."""
        if project_cwd is None:
            return []
        from lib.agent.agent_session_store import make_project_key

        project_key = await asyncio.to_thread(make_project_key, project_cwd)
        key: dict[str, Any] = {"project_key": project_key, "session_id": sdk_session_id}
        if subpath:
            key["subpath"] = subpath
        entries = await self._store.load(key)
        if (
            not entries
            and subpath.startswith("subagents/agent-")
            and callable(getattr(self._store, "list_subkeys", None))
        ):
            subpaths = await self._store.list_subkeys({"project_key": project_key, "session_id": sdk_session_id})
            leaf = subpath.rsplit("/", 1)[-1]
            nested = next(
                (path for path in subpaths if path.startswith("subagents/") and path.rsplit("/", 1)[-1] == leaf), None
            )
            if nested is not None and nested != subpath:
                entries = await self._store.load({**key, "subpath": nested})
        return [entry for entry in entries or [] if isinstance(entry, dict)]

    async def _load_raw_payloads(
        self,
        sdk_session_id: str,
        project_cwd: Path | str | None,
        subpath: str = "",
    ) -> list[dict[str, Any]]:
        """Load raw transcript payload dicts via store.load() (empty on failure)."""
        if project_cwd is None:
            logger.debug(
                "Skip raw payload load for session %s subpath=%s: project_cwd is None",
                sdk_session_id,
                subpath or "<main>",
            )
            return []
        try:
            payloads = await self._load_store_entries(sdk_session_id, project_cwd, subpath)
        except Exception:
            logger.warning(
                "Failed to load raw payloads for session %s subpath=%s",
                sdk_session_id,
                subpath or "<main>",
                exc_info=True,
            )
            return []
        return [entry for entry in payloads or [] if isinstance(entry, dict)]

    async def _load_payload_index_from_store(
        self,
        sdk_session_id: str,
        project_cwd: Path | str | None,
        subpath: str = "",
    ) -> dict[str, dict[str, Any]]:
        """Build uuid -> raw payload index by reading store payloads.

        SDK's get_session_messages_from_store / get_subagent_messages_from_store
        reconstruct SessionMessage objects that lack the per-entry timestamp /
        toolUseResult fields. We re-fetch raw payloads via store.load() and join
        on uuid so downstream consumers keep getting stable timestamps and
        structured tool results (e.g. AskUserQuestion 的 answers) without
        reparsing the chain. Works for both the main transcript
        (subpath="") and subagent subpaths.
        """
        payloads = await self._load_raw_payloads(sdk_session_id, project_cwd, subpath)
        index: dict[str, dict[str, Any]] = {}
        for entry in payloads:
            uuid = entry.get("uuid")
            if isinstance(uuid, str) and uuid:
                index[uuid] = entry
        return index

    async def read_subagent_timelines(
        self,
        sdk_session_id: str | None,
        project_cwd: Path | str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """读取 subagent 子时间线，按主线 Task tool_use id 锚定归组。

        锚定依据主 transcript 原始载荷中 Task tool_result 的
        ``toolUseResult.agentId`` 与同载荷 tool_result 块的 ``tool_use_id``。
        文件系统回退路径（``ARCREEL_SDK_SESSION_STORE=off``）的公开读取接口
        不携带该元数据，无法锚定，降级为不合并（子时间线仍在 transcript 中，
        不丢数据）。
        """
        if not sdk_session_id or not SDK_AVAILABLE:
            return {}
        if self._store is None:
            logger.info(
                "Subagent timeline merge unavailable without session store (session %s)",
                sdk_session_id,
            )
            return {}
        anchors = await self._load_agent_anchors(sdk_session_id, project_cwd)
        if not anchors:
            return {}
        try:
            agent_ids = await list_subagents_from_store(
                self._store, sdk_session_id, directory=self._coerce_cwd(project_cwd)
            )
        except Exception:
            logger.warning(
                "Failed to list subagents for session %s",
                sdk_session_id,
                exc_info=True,
            )
            return {}
        # 各 subagent 的 store 读取相互独立，并发拉取避免 N 个 subagent 时
        # 冷读路径按 N 次网络/磁盘往返的延迟串行叠加。
        results = await asyncio.gather(
            *(
                self._read_one_subagent_timeline(sdk_session_id, project_cwd, agent_id, anchors.get(agent_id))
                for agent_id in agent_ids
            )
        )
        return {tool_use_id: messages for tool_use_id, messages in results if tool_use_id and messages}

    async def _read_one_subagent_timeline(
        self,
        sdk_session_id: str,
        project_cwd: Path | str | None,
        agent_id: str,
        tool_use_id: str | None,
    ) -> tuple[str | None, list[dict[str, Any]] | None]:
        """读取单个 subagent 的子时间线；无锚点或读取失败时返回 (None, None)。"""
        if not tool_use_id:
            logger.debug("Subagent %s has no Task tool_use anchor; skipped", agent_id)
            return None, None
        try:
            messages = await get_subagent_messages_from_store(
                self._store, sdk_session_id, agent_id, directory=self._coerce_cwd(project_cwd)
            )
        except Exception:
            logger.warning(
                "Failed to read subagent %s messages for session %s",
                agent_id,
                sdk_session_id,
                exc_info=True,
            )
            return None, None
        if not messages:
            return None, None
        payload_by_uuid = await self._load_payload_index_from_store(
            sdk_session_id, project_cwd, subpath=f"subagents/agent-{agent_id}"
        )
        return tool_use_id, [self._adapt(msg, payload_by_uuid) for msg in messages]

    async def read_subagent_descriptions(
        self,
        sdk_session_id: str | None,
        project_cwd: Path | str | None,
        tool_use_ids: Collection[str],
    ) -> dict[str, str]:
        """主线原始载荷里子代理调用参数的 ``description``，按 tool_use id 取。

        压缩续接后主线沿 ``parentUuid`` 链只从续接摘要开始，摘要之前发起的
        子代理调用不在主线里；原始载荷仍保留这些调用，缺锚点的子代理据此拿到
        父代理写下的描述。找不到时不含该 id。
        """
        if not sdk_session_id or self._store is None or not tool_use_ids:
            return {}
        wanted = set(tool_use_ids)
        descriptions: dict[str, str] = {}
        for entry in await self._load_raw_payloads(sdk_session_id, project_cwd):
            for block in _content_blocks(entry):
                if block.get("type") != "tool_use" or block.get("id") not in wanted:
                    continue
                tool_input = block.get("input")
                description = tool_input.get("description") if isinstance(tool_input, dict) else None
                if isinstance(description, str) and description.strip():
                    descriptions.setdefault(block["id"], description)
        return descriptions

    async def _load_agent_anchors(
        self,
        sdk_session_id: str,
        project_cwd: Path | str | None,
    ) -> dict[str, str]:
        """agent_id → 主线 Task tool_use id 锚定映射。"""
        payloads = await self._load_raw_payloads(sdk_session_id, project_cwd)
        anchors: dict[str, str] = {}
        for entry in payloads:
            tool_use_result = entry.get("toolUseResult")
            if not isinstance(tool_use_result, dict):
                continue
            agent_id = tool_use_result.get("agentId")
            if not isinstance(agent_id, str) or not agent_id:
                continue
            tool_use_id = _first_tool_result_use_id(entry)
            if tool_use_id:
                anchors.setdefault(agent_id, tool_use_id)
        return anchors

    async def _read_via_legacy(self, sdk_session_id: str) -> list[dict[str, Any]]:
        """Filesystem fallback for ARCREEL_SDK_SESSION_STORE=off."""
        if get_session_messages is None:
            return []
        try:
            # SDK reader walks the JSONL transcript synchronously; offload so
            # SSE streaming and other coroutines aren't blocked while we wait
            # on disk I/O for large histories.
            sdk_messages = await asyncio.to_thread(get_session_messages, sdk_session_id)
        except Exception:
            logger.warning(
                "Failed to read SDK session %s",
                sdk_session_id,
                exc_info=True,
            )
            return []
        # Legacy SDK messages may carry timestamps directly; no map needed.
        return [self._adapt(m) for m in sdk_messages]

    @staticmethod
    def _coerce_cwd(project_cwd: Path | str | None) -> str | None:
        if project_cwd is None:
            return None
        return str(project_cwd)

    def _adapt(
        self,
        msg: Any,
        payload_by_uuid: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Convert SDK SessionMessage to internal dict format."""
        message_data = getattr(msg, "message", {}) or {}
        content = message_data.get("content", "") if isinstance(message_data, dict) else ""

        uuid = getattr(msg, "uuid", None)
        payload = payload_by_uuid.get(uuid) if payload_by_uuid and isinstance(uuid, str) else None

        timestamp = getattr(msg, "timestamp", None)
        if timestamp is None and payload is not None:
            payload_ts = payload.get("timestamp")
            if isinstance(payload_ts, str) and payload_ts.strip():
                timestamp = payload_ts.strip()

        result: dict[str, Any] = {
            "type": getattr(msg, "type", ""),
            "content": content,
            "uuid": uuid,
            "timestamp": timestamp,
        }

        tool_use_result = getattr(msg, "tool_use_result", None)
        if tool_use_result is None and payload is not None:
            tool_use_result = payload.get("toolUseResult")
        if isinstance(tool_use_result, dict):
            result["tool_use_result"] = tool_use_result

        # 压缩续接摘要的结构化标记只在原始载荷上，写入点据此给条目打标记。
        if payload is not None and payload.get("isCompactSummary") is True:
            result["is_compact_summary"] = True

        parent_tool_use_id = getattr(msg, "parent_tool_use_id", None)
        if parent_tool_use_id:
            result["parent_tool_use_id"] = parent_tool_use_id

        return result


def _content_blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """原始载荷 ``message.content`` 里的块；字符串正文或缺字段时为空。"""
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _first_tool_result_use_id(entry: dict[str, Any]) -> str | None:
    """Extract the first tool_result block's tool_use_id from a raw payload."""
    for block in _content_blocks(entry):
        if block.get("type") == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str) and tool_use_id:
                return tool_use_id
    return None
