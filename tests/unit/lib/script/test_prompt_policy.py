import pytest

from lib.script.prompt_policy import DRAMA_DETAIL_V1, policy_for_new_task, prompt_content_validation_enabled
from server.text_generation import TextGenerationRequest


def test_new_policy_is_scoped_to_drama_reference_video():
    assert policy_for_new_task({"content_mode": "drama", "generation_mode": "reference_video"}) == DRAMA_DETAIL_V1
    assert policy_for_new_task({"content_mode": "narration", "generation_mode": "reference_video"}) is None
    assert policy_for_new_task({"content_mode": "drama", "generation_mode": "text_to_video"}) is None


def test_policy_version_survives_queued_request_roundtrip():
    payload = TextGenerationRequest(episode=1, prompt_policy_version=DRAMA_DETAIL_V1).to_payload()

    assert payload["prompt_policy_version"] == DRAMA_DETAIL_V1
    assert TextGenerationRequest(**payload).to_payload() == payload


@pytest.mark.parametrize("value", [None, "true", "1", "false", "0", "off", "no"])
def test_content_validation_switch_does_not_change_template_policy(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ARCREEL_PROMPT_CONTENT_VALIDATION", raising=False)
    else:
        monkeypatch.setenv("ARCREEL_PROMPT_CONTENT_VALIDATION", value)
    assert prompt_content_validation_enabled() is (value in {None, "true", "1"})
    assert policy_for_new_task({"content_mode": "drama", "generation_mode": "reference_video"}) == DRAMA_DETAIL_V1
