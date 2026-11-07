"""Operation provenance propagated into domain locks and worker threads."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class FeedbackContext:
    operation_id: str
    source: str = "system"
    reason: str | None = None
    action: str | None = None
    identity_links: tuple[tuple[str, str, str, str], ...] = ()
    settings_snapshot: dict | None = None


current: ContextVar[FeedbackContext | None] = ContextVar("production_feedback_context", default=None)


@contextmanager
def operation(
    *,
    operation_id: str | None = None,
    source: str = "system",
    reason: str | None = None,
    action: str | None = None,
    settings_snapshot: dict | None = None,
):
    parent = current.get()
    token = current.set(
        FeedbackContext(
            operation_id or (parent.operation_id if parent else str(uuid4())),
            source,
            reason if reason is not None else (parent.reason if parent else None),
            action or (parent.action if parent else None),
            identity_links=parent.identity_links if parent else (),
            settings_snapshot=settings_snapshot or (parent.settings_snapshot if parent else None),
        )
    )
    try:
        yield current.get()
    finally:
        current.reset(token)


@contextmanager
def identity_link(kind: str, old: str, new: str, relation: str):
    from dataclasses import replace

    context = current.get() or FeedbackContext(str(uuid4()))
    token = current.set(replace(context, identity_links=(*context.identity_links, (kind, old, new, relation))))
    try:
        yield
    finally:
        current.reset(token)
