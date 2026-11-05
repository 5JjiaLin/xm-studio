"""Non-expiring list archives; identities, files and episode order stay intact."""

from __future__ import annotations

from typing import Any

from lib.infra.api_errors import ConflictError, NotFoundError
from lib.project.project_manager import ProjectManager, get_project_manager

MANAGEMENT_RESOURCE = "xm:project-management"


def management_state(project: dict[str, Any]) -> dict[str, Any]:
    return project.get("production_management") or {}


def assert_production_open(manager: ProjectManager | None, name: str, episodes: set[int]) -> None:
    # Queue tests and import callers may admit tasks before metadata exists. Preserve that
    # contract; an archive can only exist on a registered project.
    manager = manager or get_project_manager()
    if not manager.project_exists(name):
        return
    state = management_state(manager.load_project(name))
    if state.get("archived") or episodes.intersection(state.get("archived_episodes", [])):
        raise ConflictError("script_plan_refused", reason="请先恢复已归档的项目或单集")


def set_archive(manager: ProjectManager, name: str, archived: bool, episode: int | None = None) -> None:
    if not manager.project_exists(name):
        raise NotFoundError("project_not_found", name=name)

    def mutate(project: dict) -> None:
        if episode is not None and not any(e["episode"] == episode for e in project.get("episodes", [])):
            raise NotFoundError("episode_not_found", episode=episode)
        state = project.setdefault("production_management", {})
        if episode is None:
            state["archived"] = archived
        else:
            ids = set(state.get("archived_episodes", []))
            if archived:
                ids.add(episode)
            else:
                ids.discard(episode)
            state["archived_episodes"] = sorted(ids)

    manager.update_project(name, mutate)
