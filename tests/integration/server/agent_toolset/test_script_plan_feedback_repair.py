from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.infra.content_digest import prefixed_canonical_json_digest
from lib.script.draft_quarantine import QUARANTINE_KIND_SCRIPT_PLAN, draft_revision, quarantine_path, read_quarantine
from server.services.project.script_plan_repair import prepare_repair_context
from server.text_generation import TextGenerationError, TextGenerationRequest, generate_reference_script_plan
from tests.factories import make_video_request_facts
from tests.fakes import FakeTextGenerator, use_text_generator
from tests.integration.server.agent_tool_support import rv_script_plan_path, rv_source, rv_unit


class RepairGenerator(FakeTextGenerator):
    provider_id = "fake"
    configuration_fingerprint = "safe-config-digest"


GOLDEN_FIXTURES = json.loads((Path(__file__).parents[4] / "docs/production-mvp/fixtures/script-plan-repair-golden.json").read_text())["fixtures"]


@pytest.fixture(autouse=True)
def reference_video_plan_caps(set_video_request_facts):
    set_video_request_facts(make_video_request_facts(route="reference_video", generation_type="i2v", supported_durations=(4, 6, 8, 12), allowed_durations=(4, 6, 8, 12)))


@pytest.fixture(autouse=True)
def enable_repair(monkeypatch):
    monkeypatch.setenv("XM_SCRIPT_PLAN_REPAIR_ENABLED", "1")


def repair_reply(original, duration=12, text=None):
    return json.dumps({"status": "repaired", "reason_code": None, "replacements": [{"item_index": 0, "content_digest": prefixed_canonical_json_digest(original), "units": [{**original, "duration_seconds": duration, **({"text": text} if text else {})}]}]}, ensure_ascii=False)


async def setup_request(fake_ctx, monkeypatch, *, once=True, response=None):
    await asyncio.to_thread(rv_source, fake_ctx)
    # Default zh rate is 5/s: 48 characters exceeds 4s, fits 12s.
    original = rv_unit("@[张三]抬头。\n@[张三]{" + "这段台词保持原词" * 6 + "}", duration=4)
    fake = RepairGenerator(json.dumps({"units": [original], "new_assets": []}, ensure_ascii=False), response or repair_reply(original))
    use_text_generator(monkeypatch, fake.create)
    request = TextGenerationRequest(episode=1, repair_once=once)
    if once:
        source, context = await prepare_repair_context(request, project_name="demo", projects=fake_ctx.pm, config_resolver=fake_ctx.services.capabilities)
        request = replace(request, source=source, repair_context=context)
    return request, original, fake


async def execute(fake_ctx, request):
    return await generate_reference_script_plan(request, project_name="demo", projects=fake_ctx.pm, config_resolver=fake_ctx.services.capabilities)


async def test_one_repair_passes_and_waits_for_confirmation(fake_ctx, monkeypatch):
    request, original, fake = await setup_request(fake_ctx, monkeypatch)
    await execute(fake_ctx, request)
    assert len(fake.requests) == 2
    assert all(r.response_schema is None for r in fake.requests)
    assert fake.require_complete == [True, True]
    plan = json.loads(rv_script_plan_path(fake_ctx).read_text())
    assert plan["units"][0]["text"] == original["text"]
    assert plan["units"][0]["duration_seconds"] == 12
    assert not (fake_ctx.project_path / "scripts/episode_1.json").exists()
    assert not quarantine_path(fake_ctx.project_path, 1, QUARANTINE_KIND_SCRIPT_PLAN).exists()


async def test_default_does_not_repair(fake_ctx, monkeypatch):
    request, _original, fake = await setup_request(fake_ctx, monkeypatch, once=False)
    with pytest.raises(TextGenerationError):
        await execute(fake_ctx, request)
    assert len(fake.requests) == 1


@pytest.mark.parametrize("policy", [None, "drama_detail_v1"])
async def test_tool_repair_preserves_original_policy_after_project_mode_changes(fake_ctx, monkeypatch, policy):
    from tests.integration.server.agent_tool_support import run_declared_tool

    request, original, fake = await setup_request(fake_ctx, monkeypatch, once=False)
    with pytest.raises(TextGenerationError):
        await execute(fake_ctx, replace(request, prompt_policy_version=policy))
    draft = read_quarantine(fake_ctx.project_path, 1, QUARANTINE_KIND_SCRIPT_PLAN)
    assert draft.meta.get("prompt_policy_version") == policy
    # The current project would select the new policy for a NEW task. A repair
    # must still use the persisted draft policy, including legacy None.
    fake_ctx.pm.project_payload["content_mode"] = "drama"
    (fake_ctx.project_path / "project.json").write_text(json.dumps(fake_ctx.pm.project_payload))
    outcome = await run_declared_tool("generate_script_plan", fake_ctx, {
        "episode_id": 1, "repair_draft_revision": draft_revision(draft),
    })
    assert outcome.problem is None, outcome.problem
    assert len(fake.requests) == 2
    saved = json.loads(rv_script_plan_path(fake_ctx).read_text())
    assert saved.get("prompt_policy_version") == policy
    assert saved["units"][0]["text"] == original["text"]
    assert not (fake_ctx.project_path / "scripts/episode_1.json").exists()


@pytest.mark.parametrize("response", [TimeoutError("unknown acceptance"), "{}", '{"replacements": []}'])
async def test_failed_repair_keeps_original_draft_without_loop(fake_ctx, monkeypatch, response):
    request, original, fake = await setup_request(fake_ctx, monkeypatch, response=response)
    with pytest.raises(TextGenerationError):
        await execute(fake_ctx, request)
    draft = read_quarantine(fake_ctx.project_path, 1, QUARANTINE_KIND_SCRIPT_PLAN)
    assert draft.content["units"] == [original]
    assert len(fake.requests) == 2
    assert not rv_script_plan_path(fake_ctx).exists()


async def test_explicit_repair_revalidates_and_sends_report_once(fake_ctx, monkeypatch):
    request, original, first = await setup_request(fake_ctx, monkeypatch, once=False)
    with pytest.raises(TextGenerationError):
        await execute(fake_ctx, request)
    draft = read_quarantine(fake_ctx.project_path, 1, QUARANTINE_KIND_SCRIPT_PLAN)
    fake = RepairGenerator(repair_reply(original))
    use_text_generator(monkeypatch, fake.create)
    request = replace(request, repair_draft_revision=draft_revision(draft))
    source, context = await prepare_repair_context(request, project_name="demo", projects=fake_ctx.pm, config_resolver=fake_ctx.services.capabilities)
    await execute(fake_ctx, replace(request, source=source, repair_context=context))
    assert len(fake.requests) == 1
    assert '"estimated_spoken_seconds"' in fake.requests[0].prompt
    assert '"dialogue_overload"' in fake.requests[0].prompt
    assert len(first.requests) == 1


async def test_stale_source_prevents_any_generation(fake_ctx, monkeypatch):
    request, _original, fake = await setup_request(fake_ctx, monkeypatch)
    (fake_ctx.project_path / "source/episode_1.txt").write_text("新的源文")
    with pytest.raises(TextGenerationError, match="repair_basis_changed"):
        await execute(fake_ctx, request)
    assert fake.requests == []


async def interrupted_attempt(fake_ctx, monkeypatch, *, phase="revalidating", committed=False):
    from lib.project.task_project_claim import claim_task_project
    from server.services.project.script_plan_repair import RepairEvidence

    request, original, fake = await setup_request(fake_ctx, monkeypatch)
    record = RepairEvidence.record
    interrupted = False

    async def interrupt_after_persistence(self, next_phase, **updates):
        nonlocal interrupted
        await record(self, next_phase, **updates)
        if not interrupted and ((committed and next_phase == "completed") or (not committed and next_phase == phase and "responses" in updates)):
            interrupted = True
            raise asyncio.CancelledError()

    monkeypatch.setattr(RepairEvidence, "record", interrupt_after_persistence)
    with claim_task_project("original-task", "demo"), pytest.raises(asyncio.CancelledError):
        await execute(fake_ctx, request)
    monkeypatch.setattr(RepairEvidence, "record", record)
    return request, original, fake


async def local_recheck(fake_ctx):
    from server.services.project.script_plan_repair import prepare_recheck_request

    return await prepare_recheck_request(
        TextGenerationRequest(episode=1, repair_recheck_task_id="original-task"),
        project_name="demo", projects=fake_ctx.pm,
    )


async def test_complete_repair_response_survives_interrupt_and_rechecks_without_ai(fake_ctx, monkeypatch):
    from lib.project.task_project_claim import claim_task_project

    _, original, fake = await interrupted_attempt(fake_ctx, monkeypatch)
    assert len(fake.requests) == 2
    assert not rv_script_plan_path(fake_ctx).exists()
    request = await local_recheck(fake_ctx)
    with claim_task_project("local-recheck-task", "demo"):
        await execute(fake_ctx, request)
    assert len(fake.requests) == 2
    assert json.loads(rv_script_plan_path(fake_ctx).read_text())["units"][0]["text"] == original["text"]
    assert not (fake_ctx.project_path / "scripts/episode_1.json").exists()


async def test_local_recheck_remains_available_when_paid_repair_is_disabled(fake_ctx, monkeypatch):
    from lib.project.task_project_claim import claim_task_project

    _, original, fake = await interrupted_attempt(fake_ctx, monkeypatch)
    request = await local_recheck(fake_ctx)
    monkeypatch.setenv("XM_SCRIPT_PLAN_REPAIR_ENABLED", "0")
    with claim_task_project("local-recheck-switch-off", "demo"):
        await execute(fake_ctx, request)
    assert len(fake.requests) == 2
    assert json.loads(rv_script_plan_path(fake_ctx).read_text())["units"][0]["text"] == original["text"]


async def test_initial_valid_response_recovers_without_a_repair_call(fake_ctx, monkeypatch):
    from lib.project.task_project_claim import claim_task_project
    from server.services.project.script_plan_repair import RepairEvidence

    request, original, fake = await setup_request(fake_ctx, monkeypatch)
    fake._responses[0] = json.dumps({"units": [{**original, "duration_seconds": 12}], "new_assets": []})
    record = RepairEvidence.record

    async def interrupt(self, phase, **updates):
        await record(self, phase, **updates)
        if phase == "validating" and "responses" in updates:
            raise asyncio.CancelledError()

    monkeypatch.setattr(RepairEvidence, "record", interrupt)
    with claim_task_project("original-task", "demo"), pytest.raises(asyncio.CancelledError):
        await execute(fake_ctx, request)
    monkeypatch.setattr(RepairEvidence, "record", record)
    request = await local_recheck(fake_ctx)
    with claim_task_project("local-recheck", "demo"):
        await execute(fake_ctx, request)
    assert len(fake.requests) == 1
    assert json.loads(rv_script_plan_path(fake_ctx).read_text())["units"][0]["duration_seconds"] == 12


async def test_committed_output_with_lost_receipt_is_recognized_without_rewriting(fake_ctx, monkeypatch):
    from lib.project.task_project_claim import claim_task_project

    _, _, fake = await interrupted_attempt(fake_ctx, monkeypatch, committed=True)
    original_file = rv_script_plan_path(fake_ctx).read_bytes()
    request = await local_recheck(fake_ctx)
    with claim_task_project("receipt-recheck", "demo"):
        await execute(fake_ctx, request)
    assert len(fake.requests) == 2
    assert rv_script_plan_path(fake_ctx).read_bytes() == original_file


async def test_lost_receipt_does_not_require_an_available_text_generator(fake_ctx, monkeypatch):
    from lib.project.task_project_claim import claim_task_project

    _, _, fake = await interrupted_attempt(fake_ctx, monkeypatch, committed=True)
    request = await local_recheck(fake_ctx)
    before = rv_script_plan_path(fake_ctx).read_bytes()

    async def unavailable(*args, **kwargs):
        raise ValueError("current provider configuration unavailable")

    use_text_generator(monkeypatch, unavailable)
    with claim_task_project("receipt-without-provider", "demo"):
        result = await execute(fake_ctx, request)
    assert result.message
    assert len(fake.requests) == 2
    assert rv_script_plan_path(fake_ctx).read_bytes() == before


async def test_changed_parent_evidence_is_rejected_before_recovery(fake_ctx, monkeypatch):
    from lib.project.task_project_claim import claim_task_project
    from server.services.project.script_plan_repair import RepairEvidence

    initial, _, fake = await interrupted_attempt(fake_ctx, monkeypatch)
    request = await local_recheck(fake_ctx)
    with claim_task_project("original-task", "demo"):
        evidence = RepairEvidence(fake_ctx.project_path, 1, json.loads(initial.repair_context))
    evidence.path.write_text(evidence.path.read_text() + "\n", encoding="utf-8")
    with claim_task_project("stale-evidence-recovery", "demo"), pytest.raises(TextGenerationError, match="repair_evidence_changed"):
        await execute(fake_ctx, request)
    assert len(fake.requests) == 2
    assert not rv_script_plan_path(fake_ctx).exists()


async def test_model_refusal_keeps_draft_and_does_not_offer_repeating_local_recheck(fake_ctx, monkeypatch):
    from lib.project.task_project_claim import claim_task_project
    from server.services.project.script_plan_repair import read_repair_progress

    refusal = json.dumps({"status": "unrepairable", "replacements": [], "reason_code": "cannot_preserve_content"})
    request, original, fake = await setup_request(fake_ctx, monkeypatch, response=refusal)
    with claim_task_project("refusal-task", "demo"), pytest.raises(TextGenerationError, match="repair_response_unrepairable"):
        await execute(fake_ctx, request)
    assert len(fake.requests) == 2
    assert read_quarantine(fake_ctx.project_path, 1, QUARANTINE_KIND_SCRIPT_PLAN).content["units"] == [original]
    progress = read_repair_progress(fake_ctx.project_path, 1)
    assert not progress["can_recheck"]
    assert "无法" in progress["recheck_reason"]
    assert not rv_script_plan_path(fake_ctx).exists()


@pytest.mark.parametrize("mutation", ["source", "draft", "configuration", "formal"])
async def test_recovery_rejects_changed_inputs_and_preserves_updates(fake_ctx, monkeypatch, mutation):
    from lib.project.task_project_claim import claim_task_project

    _, _, fake = await interrupted_attempt(fake_ctx, monkeypatch)
    request = await local_recheck(fake_ctx)
    if mutation == "source":
        path = fake_ctx.project_path / "source/episode_1.txt"
        path.write_text("源文已由用户更新", encoding="utf-8")
    elif mutation == "draft":
        path = quarantine_path(fake_ctx.project_path, 1, QUARANTINE_KIND_SCRIPT_PLAN)
        path.write_text(path.read_text().replace("这段台词", "人工台词"), encoding="utf-8")
    elif mutation == "configuration":
        path = None
        fake.configuration_fingerprint = "new-configuration"
    else:
        path = rv_script_plan_path(fake_ctx)
        path.write_text('{"units": [], "new_assets": []}', encoding="utf-8")
    saved = path.read_bytes() if path else None
    with claim_task_project("recheck-stale", "demo"), pytest.raises(TextGenerationError, match=r"repair_(basis_changed|revision_conflict)"):
        await execute(fake_ctx, request)
    assert len(fake.requests) == 2
    if path:
        assert path.read_bytes() == saved


async def test_same_task_cannot_replay_after_unknown_request(fake_ctx, monkeypatch):
    from lib.project.task_project_claim import claim_task_project
    from server.services.project.script_plan_repair import PlanRepairError

    request, _, fake = await setup_request(fake_ctx, monkeypatch)
    fake._responses[0] = TimeoutError("request acceptance unknown")
    with claim_task_project("original-task", "demo"), pytest.raises(TextGenerationError):
        await execute(fake_ctx, request)
    with claim_task_project("original-task", "demo"), pytest.raises(TextGenerationError, match="repair_attempt_exists"):
        await execute(fake_ctx, request)
    with pytest.raises(PlanRepairError, match="repair_response_unknown"):
        await local_recheck(fake_ctx)
    assert len(fake.requests) == 1


async def test_dry_run_has_no_provider_calls_or_evidence(fake_ctx, monkeypatch):
    from lib.episode.episode_paths import episode_drafts_dir

    request, _, fake = await setup_request(fake_ctx, monkeypatch)
    result = await execute(fake_ctx, replace(request, dry_run=True))
    assert "DRY RUN" in result.message
    assert fake.requests == []
    assert not (episode_drafts_dir(fake_ctx.project_path, 1) / "script_plan_repair").exists()


async def test_disabled_switch_rejects_opt_in_but_preserves_legacy_path(fake_ctx, monkeypatch):
    request, _, fake = await setup_request(fake_ctx, monkeypatch)
    monkeypatch.setenv("XM_SCRIPT_PLAN_REPAIR_ENABLED", "0")
    with pytest.raises(TextGenerationError, match="repair_disabled"):
        await execute(fake_ctx, request)
    assert fake.requests == []
    with pytest.raises(TextGenerationError, match="草稿"):
        await execute(fake_ctx, replace(request, repair_once=False, repair_context=None))
    assert len(fake.requests) == 1


async def test_existing_downstream_script_blocks_repair_and_leaves_every_file(fake_ctx, monkeypatch):
    from server.services.project.script_plan_repair import PlanRepairError

    _, _, fake = await setup_request(fake_ctx, monkeypatch, once=False)
    script = fake_ctx.project_path / "scripts/episode_1.json"
    script.parent.mkdir(exist_ok=True)
    script.write_text('{"video_units": [{"text": "人工提示词"}]}', encoding="utf-8")
    before = {p.relative_to(fake_ctx.project_path): p.read_bytes() for p in fake_ctx.project_path.rglob("*") if p.is_file()}
    with pytest.raises(PlanRepairError, match="repair_downstream_exists"):
        await prepare_repair_context(TextGenerationRequest(episode=1, repair_once=True), project_name="demo", projects=fake_ctx.pm, config_resolver=fake_ctx.services.capabilities)
    after = {p.relative_to(fake_ctx.project_path): p.read_bytes() for p in fake_ctx.project_path.rglob("*") if p.is_file()}
    assert before == after
    assert fake.requests == []


async def test_queue_round_trip_retains_repair_mode_and_waits_for_original_confirmation(fake_ctx, monkeypatch):
    from tests.integration.server.agent_tool_support import run_declared_tool

    _, original, fake = await setup_request(fake_ctx, monkeypatch, once=False)
    outcome = await run_declared_tool("generate_script_plan", fake_ctx, {"episode_id": 1, "repair_once": True})
    assert outcome.problem is None, outcome.problem
    assert len(fake.requests) == 2
    plan = json.loads(rv_script_plan_path(fake_ctx).read_text())
    assert plan["units"][0]["text"] == original["text"]
    assert not (fake_ctx.project_path / "scripts/episode_1.json").exists()


async def test_double_submission_joins_the_same_queue_task_without_extra_calls(fake_ctx, monkeypatch):
    from server import tool_runtime
    from tests.integration.server.agent_tool_support import run_declared_tool

    _, _, fake = await setup_request(fake_ctx, monkeypatch, once=False)
    entered = asyncio.Event()
    release = asyncio.Event()
    joined = asyncio.Event()
    task_ids = []
    original_generate = fake.generate
    original_wait = tool_runtime.wait_for_task

    async def wait_on_provider(*args, **kwargs):
        if not fake.requests:
            entered.set()
            await release.wait()
        return await original_generate(*args, **kwargs)

    async def observe_wait(task_id, *, queue):
        task_ids.append(task_id)
        if len(task_ids) == 2:
            joined.set()
        return await original_wait(task_id, queue=queue)

    monkeypatch.setattr(fake, "generate", wait_on_provider)
    monkeypatch.setattr(tool_runtime, "wait_for_task", observe_wait)
    attempts = [asyncio.create_task(run_declared_tool("generate_script_plan", fake_ctx, {"episode_id": 1, "repair_once": True}))]
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        attempts.append(asyncio.create_task(run_declared_tool("generate_script_plan", fake_ctx, {"episode_id": 1, "repair_once": True})))
        await asyncio.wait_for(joined.wait(), timeout=5)
        release.set()
        outcomes = await asyncio.gather(*attempts)
    finally:
        release.set()
        await asyncio.gather(*attempts, return_exceptions=True)
    assert all(outcome.problem is None for outcome in outcomes)
    assert len(task_ids) == 2
    assert task_ids[0] == task_ids[1]
    assert len(fake.requests) == 2


async def test_disabling_repair_after_initial_response_prevents_a_second_call(fake_ctx, monkeypatch):
    request, original, fake = await setup_request(fake_ctx, monkeypatch)
    original_generate = fake.generate

    async def disable_after_response(*args, **kwargs):
        result = await original_generate(*args, **kwargs)
        monkeypatch.setenv("XM_SCRIPT_PLAN_REPAIR_ENABLED", "0")
        return result

    monkeypatch.setattr(fake, "generate", disable_after_response)
    with pytest.raises(TextGenerationError, match="repair_disabled"):
        await execute(fake_ctx, request)
    assert len(fake.requests) == 1
    assert read_quarantine(fake_ctx.project_path, 1, QUARANTINE_KIND_SCRIPT_PLAN).content["units"] == [original]
    assert not rv_script_plan_path(fake_ctx).exists()


@pytest.mark.parametrize("phase", ["revalidating", "completed"])
async def test_queue_saved_response_or_lost_receipt_recovers_locally(fake_ctx, monkeypatch, phase):
    from server.services.project.script_plan_repair import RepairEvidence
    from tests.integration.server.agent_tool_support import run_declared_tool

    _, original, fake = await setup_request(fake_ctx, monkeypatch, once=False)
    record = RepairEvidence.record
    original_task = None

    async def fail_after_saved(self, next_phase, **updates):
        nonlocal original_task
        await record(self, next_phase, **updates)
        if original_task is None and next_phase == phase:
            original_task = self.data["task_id"]
            raise RuntimeError("simulated process loss after persistence")

    monkeypatch.setattr(RepairEvidence, "record", fail_after_saved)
    outcome = await run_declared_tool("generate_script_plan", fake_ctx, {"episode_id": 1, "repair_once": True})
    assert outcome.problem is not None
    assert original_task
    assert len(fake.requests) == 2
    before = rv_script_plan_path(fake_ctx).read_bytes() if phase == "completed" else None
    monkeypatch.setattr(RepairEvidence, "record", record)
    recovered = await run_declared_tool("generate_script_plan", fake_ctx, {"episode_id": 1, "repair_recheck_task_id": original_task})
    assert recovered.problem is None, recovered.problem
    assert len(fake.requests) == 2
    if before is not None:
        assert rv_script_plan_path(fake_ctx).read_bytes() == before
    assert json.loads(rv_script_plan_path(fake_ctx).read_text())["units"][0]["text"] == original["text"]
    assert not (fake_ctx.project_path / "scripts/episode_1.json").exists()


async def test_queue_recheck_rejects_a_running_or_foreign_task_without_ai(fake_ctx, monkeypatch):
    from tests.integration.server.agent_tool_support import run_declared_tool

    _, _, fake = await setup_request(fake_ctx, monkeypatch, once=False)
    outcome = await run_declared_tool("generate_script_plan", fake_ctx, {"episode_id": 1, "repair_recheck_task_id": "not-this-project-task"})
    assert outcome.problem is not None
    assert outcome.problem.code == "generation_refused"
    assert fake.requests == []


async def test_default_payload_omits_all_new_optional_fields():
    from lib.generation.generation_queue import text_task_request_facts

    old = {"episode": 1, "source": None, "instructions": None, "dry_run": False, "entry_ids": [], "rewrite": False, "overwrite_revision": None, "regenerate": False}
    assert TextGenerationRequest(episode=1).to_payload() == old
    assert text_task_request_facts(old) == text_task_request_facts(TextGenerationRequest(episode=1).to_payload())


async def test_corrupted_attempt_evidence_never_replays_provider_request(fake_ctx, monkeypatch):
    from lib.project.task_project_claim import claim_task_project
    from server.services.project.script_plan_repair import RepairEvidence

    request, _, fake = await setup_request(fake_ctx, monkeypatch)
    with claim_task_project("same-task", "demo"):
        evidence = RepairEvidence(fake_ctx.project_path, 1, json.loads(request.repair_context))
        await evidence.begin()
        evidence.path.write_text("{incomplete evidence", encoding="utf-8")
        with pytest.raises(TextGenerationError, match="repair_evidence_corrupted"):
            await execute(fake_ctx, request)
    assert fake.requests == []


@pytest.mark.parametrize("sample", GOLDEN_FIXTURES, ids=lambda sample: sample["id"])
async def test_golden_default_path_is_identical_with_repair_switch_off_or_on(fake_ctx, monkeypatch, set_video_request_facts, sample):
    await asyncio.to_thread(rv_source, fake_ctx)
    project = fake_ctx.pm.project_payload
    project["source_language"] = sample["language"]
    if derivative := sample.get("derivative"):
        project["characters"][derivative["owner"]].setdefault("derivatives", {})[derivative["name"]] = {"description": derivative["description"]}
    project_json = json.dumps(project, ensure_ascii=False)
    source_path = fake_ctx.project_path / "source/episode_1.txt"
    source_path.write_text(sample["source"], encoding="utf-8")
    if durations := sample.get("durations"):
        set_video_request_facts(make_video_request_facts(route="reference_video", generation_type="i2v", supported_durations=tuple(durations), allowed_durations=tuple(durations)))
    response = json.dumps({"units": sample["units"], "new_assets": sample["new_assets"]}, ensure_ascii=False)
    snapshots = []
    for enabled in ("0", "1"):
        monkeypatch.setenv("XM_SCRIPT_PLAN_REPAIR_ENABLED", enabled)
        fake_ctx.pm.project_payload = json.loads(project_json)
        (fake_ctx.project_path / "project.json").write_text(project_json)
        rv_script_plan_path(fake_ctx).unlink(missing_ok=True)
        quarantine_path(fake_ctx.project_path, 1, QUARANTINE_KIND_SCRIPT_PLAN).unlink(missing_ok=True)
        fake = RepairGenerator(response)
        use_text_generator(monkeypatch, fake.create)
        error = None
        try:
            await execute(fake_ctx, TextGenerationRequest(episode=1))
        except TextGenerationError as exc:
            error = str(exc)
        assert len(fake.requests) == 1
        sent = fake.requests[0]
        draft = read_quarantine(fake_ctx.project_path, 1, QUARANTINE_KIND_SCRIPT_PLAN)
        formal = rv_script_plan_path(fake_ctx)
        snapshots.append((
            replace(sent, response_schema=None), sent.response_schema.model_json_schema(), fake.require_complete,
            error, json.loads(formal.read_text())["units"] if formal.exists() else None,
            (draft.content, draft.violations) if draft else None,
        ))
        assert (error is None) == (sample["expected_calls"] == 1 and sample["expected_result"] == "success")
        assert source_path.read_text() == sample["source"]
        after_project = fake_ctx.pm.load_project("demo")
        assert {kind: after_project.get(kind) for kind in ("characters", "scenes", "props")} == {
            kind: project.get(kind) for kind in ("characters", "scenes", "props")
        }
        assert not (fake_ctx.project_path / "scripts/episode_1.json").exists()
        assert not (fake_ctx.project_path / "drafts/episode_1/script_plan_repair").exists()
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("sample", GOLDEN_FIXTURES, ids=lambda sample: sample["id"])
async def test_golden_scripts_pass_the_full_planning_path_or_stop_safely(fake_ctx, monkeypatch, set_video_request_facts, sample):
    await asyncio.to_thread(rv_source, fake_ctx)
    project = fake_ctx.pm.project_payload
    project["source_language"] = sample["language"]
    if derivative := sample.get("derivative"):
        project["characters"][derivative["owner"]].setdefault("derivatives", {})[derivative["name"]] = {"description": derivative["description"]}
    (fake_ctx.project_path / "project.json").write_text(json.dumps(project, ensure_ascii=False))
    source_path = fake_ctx.project_path / "source/episode_1.txt"
    source_path.write_text(sample["source"], encoding="utf-8")
    if durations := sample.get("durations"):
        set_video_request_facts(make_video_request_facts(route="reference_video", generation_type="i2v", supported_durations=tuple(durations), allowed_durations=tuple(durations)))
    initial = {"units": sample["units"], "new_assets": sample["new_assets"]}
    replies = [json.dumps(initial, ensure_ascii=False)]
    if sample["expected_calls"] == 2:
        repairs = sample.get("repairs")
        response = {"status": "unrepairable", "reason_code": "cannot_preserve_content", "replacements": []} if repairs is None else {
            "status": "repaired", "reason_code": None, "replacements": [
                {**block, "content_digest": prefixed_canonical_json_digest(sample["units"][block["item_index"]])}
                for block in repairs
            ],
        }
        replies.append(json.dumps(response, ensure_ascii=False))
    fake = RepairGenerator(*replies)
    use_text_generator(monkeypatch, fake.create)
    request = TextGenerationRequest(episode=1, repair_once=True)
    source, context = await prepare_repair_context(request, project_name="demo", projects=fake_ctx.pm, config_resolver=fake_ctx.services.capabilities)
    request = replace(request, source=source, repair_context=context)
    before_assets = json.dumps({kind: project.get(kind) for kind in ("characters", "scenes", "props")}, sort_keys=True)
    if sample["expected_result"] == "success":
        await execute(fake_ctx, request)
        actual = json.loads(rv_script_plan_path(fake_ctx).read_text())
        replacements = {block["item_index"]: block["units"] for block in (sample.get("repairs") or [])}
        expected = [unit for index, original in enumerate(sample["units"]) for unit in replacements.get(index, [original])]
        assert [{key: unit[key] for key in ("source_text", "text", "duration_seconds")} for unit in actual["units"]] == expected
    else:
        code = "repair_response_unrepairable" if sample["expected_result"] == "refusal" else "repair_not_supported"
        with pytest.raises(TextGenerationError, match=code):
            await execute(fake_ctx, request)
        assert not rv_script_plan_path(fake_ctx).exists()
        assert read_quarantine(fake_ctx.project_path, 1, QUARANTINE_KIND_SCRIPT_PLAN).content["units"] == sample["units"]
    assert len(fake.requests) == sample["expected_calls"]
    if sample["expected_calls"] == 2:
            assert json.dumps(sample["source"], ensure_ascii=False) in fake.requests[-1].prompt
    else:
        assert sample["source"] in source_path.read_text(encoding="utf-8")
    after_project = fake_ctx.pm.load_project("demo")
    assert json.dumps({kind: after_project.get(kind) for kind in ("characters", "scenes", "props")}, sort_keys=True) == before_assets
    assert source_path.read_text() == sample["source"]
    assert not (fake_ctx.project_path / "scripts/episode_1.json").exists()
