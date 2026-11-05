"""Freeze attribution at provider-call start and expose read-only cost totals."""

import json

from sqlalchemy import select, update

from lib.db.base import DEFAULT_USER_ID, utc_now
from lib.db.models.api_call import ApiCall
from lib.db.models.task import Task
from lib.db.repositories.usage_repo import UsageRepository


def task(task_id, episode=1, **kwargs):
    return Task(
        task_id=task_id,
        project_name="demo",
        task_type="character",
        media_type="image",
        resource_id="hero",
        payload_json=json.dumps({"billing_episode_id": episode}),
        status="succeeded",
        queued_at=utc_now(),
        updated_at=utc_now(),
        user_id=DEFAULT_USER_ID,
        **kwargs,
    )


async def test_snapshot_is_frozen_and_shared_asset_regeneration_belongs_to_initiating_episode(async_session):
    async_session.add_all([task("one"), task("two", 2)])
    await async_session.commit()
    repo = UsageRepository(async_session)
    one = await repo.start_call(project_name="demo", call_type="image", model="m", provider="gemini", task_id="one")
    two = await repo.start_call(project_name="demo", call_type="image", model="m", provider="gemini", task_id="two")
    await async_session.execute(
        update(Task).where(Task.task_id == "one").values(payload_json='{"billing_episode_id": 99}')
    )
    await async_session.execute(
        update(ApiCall).where(ApiCall.id.in_([one, two])).values(status="failed", cost_amount=0.25, currency="CNY")
    )
    await async_session.commit()
    before = len((await async_session.execute(select(ApiCall))).scalars().all())
    result = await repo.production_costs("demo", {"one.json": 1, "two.json": 2})
    assert [e["amounts"] for e in result["episodes"]] == [{"CNY": 0.25}, {"CNY": 0.25}]
    assert [e["reroll_count"] for e in result["episodes"]] == [0, 1]
    assert len((await async_session.execute(select(ApiCall))).scalars().all()) == before
    assert (await async_session.get(ApiCall, one)).episode_id == 1


async def test_foreign_project_and_user_tasks_cannot_attribute_calls(async_session):
    async_session.add(task("other"))
    await async_session.commit()
    repo = UsageRepository(async_session)
    call = await repo.start_call(project_name="other", call_type="text", model="m", provider="gemini", task_id="other")
    assert (await async_session.get(ApiCall, call)).episode_id is None
    result = await repo.production_costs("other", {"one.json": 1})
    assert result["unassigned"]["pending_count"] == 1
