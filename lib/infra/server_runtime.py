"""Single-process admission and drain state, independent of HTTP or service hosts.

Maintenance blocks new work; it never cancels admitted work or its result writes.
The company launcher owns persistent markers and the local control channel.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


class MaintenanceError(RuntimeError):
    """New work cannot be admitted while the server is draining."""


@dataclass
class ServerRuntime:
    maintenance: bool = False
    inspection: bool = False
    storage_critical: bool = False
    initialized: bool = False
    heartbeat: float = 0.0
    activities: Counter[str] = field(default_factory=Counter)

    @property
    def paused(self) -> bool:
        return self.maintenance or self.inspection or self.storage_critical

    def require_admission(self) -> None:
        if self.paused:
            if self.storage_critical and not self.maintenance and not self.inspection:
                raise MaintenanceError("服务器磁盘空间不足，已暂停新增任务；请管理员清理或扩容。")
            raise MaintenanceError("服务器维护中，请稍后重试；已开始的任务继续保存结果。")

    @contextmanager
    def activity(self, category: str) -> Iterator[None]:
        self.activities[category] += 1
        try:
            yield
        finally:
            self.activities[category] -= 1
            if not self.activities[category]:
                del self.activities[category]

    def tick(self) -> None:
        self.heartbeat = time.monotonic()

    def track_task(self, category: str, coroutine) -> asyncio.Task:
        """Register before scheduling, so drain cannot miss a not-yet-run task."""
        self.activities[category] += 1
        try:
            task = asyncio.create_task(coroutine)
        except BaseException:
            self.activities[category] -= 1
            coroutine.close()
            raise

        def finished(_task):
            self.activities[category] -= 1
            if not self.activities[category]:
                del self.activities[category]

        task.add_done_callback(finished)
        return task

    def reset(self, *, maintenance: bool = False) -> None:
        if self.activities:
            raise RuntimeError("Cannot reset server runtime while work is active")
        self.maintenance = maintenance
        self.storage_critical = False
        self.inspection = os.environ.get("XMSTUDIO_INSPECTION", "false").lower() == "true"
        self.initialized = False
        self.heartbeat = 0.0
        self.activities.clear()


runtime = ServerRuntime()
