from lib.script.reference_video.prompt_render import render_video_unit_prompt
from lib.script.reference_video.voice_settings import VoiceRenderSettings
from lib.script.script_models import ReferenceResource


def test_angle_reference_binds_to_base_speaker_and_preserves_custom_creative_text():
    project = {
        "characters": {
            "林清": {
                "voice_style": "清亮女声",
                "derivatives": {"视角-正面特写": {"description": "同一人物正面特写"}},
            }
        },
        "scenes": {},
        "props": {},
    }
    unit = {"text": "@[林清/视角-正面特写]抬头。@[林清]{你好}", "generation_prompt": "<林清>抬头。"}
    rendered = render_video_unit_prompt(
        unit,
        project,
        VoiceRenderSettings(),
        request_references=[ReferenceResource(type="character", name="林清/视角-正面特写")],
    )
    assert "<林清/视角-正面特写>@图片1" in rendered.declarations
    assert "<林清/视角-正面特写>是<林清>" in rendered.declarations
    assert "同一人物、同一服装" in rendered.declarations
    assert rendered.creative_text == unit["generation_prompt"]
    assert "@图片2" not in rendered.prompt
