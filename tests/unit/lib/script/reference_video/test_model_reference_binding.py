from lib.script.reference_video.model_reference_binding import (
    normalize_model_visual_references,
    repair_model_mention_closers,
)
from lib.script.reference_video.text_parser import extract_mentions


def test_repair_exact_registered_closer_preserves_other_content():
    original = "@[大厅]，@[吉迪恩/军服>站定。\n<吉迪恩/军服>在左。\n@[吉迪恩]{台词 @[吉迪恩/军服>}"
    fixed, repairs = repair_model_mention_closers(original, registered_names=("大厅", "吉迪恩/军服"))
    assert fixed == original.replace("@[吉迪恩/军服>站定", "@[吉迪恩/军服]站定")
    assert repairs == [(0, "吉迪恩/军服")]
    assert extract_mentions(fixed.splitlines()[0]) == ["大厅", "吉迪恩/军服"]
    assert repair_model_mention_closers(fixed, registered_names=("大厅", "吉迪恩/军服"))[1] == []


def test_repair_rejects_unknown_ambiguous_nested_and_speech_lines():
    original = "@[未知> @[重名> <说明@[角色>> @[角色>{台词} @[前@[角色>"
    fixed, repairs = repair_model_mention_closers(original, registered_names=("重名", "重名", "角色"))
    assert fixed == original
    assert repairs == []
    original = "@[未知> @[重名> <说明@[角色>> @[前@[角色>"
    fixed, repairs = repair_model_mention_closers(original, registered_names=("重名", "重名", "角色"))
    assert fixed == original
    assert repairs == []


def test_repair_keeps_unknown_ambiguous_and_angle_text_unchanged():
    original = "@[未知>\n@[重名>\n<未闭合 @[角色>\n@[角色>{台词}\n"
    fixed, repairs = repair_model_mention_closers(original, registered_names=("重名", "重名", "角色"))
    assert fixed == original
    assert repairs == []


def body(action):
    return f"【本组设定】\n@[石殿]\n【开场承接】\n吉迪恩站在左侧。\n【分段时间轴】\n【0-8s】\n{action}\n【组尾交接】\n吉迪恩站定。\n【必要限制】\n无。"


def test_bind_confirmed_exact_names_in_actions_without_touching_speech_or_states():
    text = body("吉迪恩面对埃利亚斯，手握@[木箱]。@[吉迪恩]{埃利亚斯，过来。}")
    fixed = normalize_model_visual_references(
        text, required_source_text="@[吉迪恩] 面对 @[埃利亚斯]，握着@[木箱]。",
        registered_names=("吉迪恩", "埃利亚斯", "木箱", "石殿"),
    )
    assert "@[吉迪恩]面对@[埃利亚斯]" in fixed
    assert "@[吉迪恩]{埃利亚斯，过来。}" in fixed
    assert fixed.split("【分段时间轴】")[0] == text.split("【分段时间轴】")[0]
    assert fixed.split("【组尾交接】")[1] == text.split("【组尾交接】")[1]
    assert normalize_model_visual_references(
        fixed, required_source_text="@[吉迪恩] @[埃利亚斯] @[木箱]",
        registered_names=("吉迪恩", "埃利亚斯", "木箱", "石殿"),
    ) == fixed


def test_no_inference_from_speech_aliases_offscreen_or_longer_names():
    text = body("吉迪恩在画外。埃利亚斯回忆吉迪恩。吉迪恩父亲转身。GIDEON站定。@[吉迪恩]{吉迪恩。}")
    assert normalize_model_visual_references(
        text, required_source_text="@[吉迪恩]站立。",
        registered_names=("吉迪恩", "吉迪恩父亲", "埃利亚斯"),
    ) == text


def test_do_not_replace_existing_angle_or_infer_derivative_from_base():
    text = body("@[吉迪恩/视角-正面特写]转头。吉迪恩站定。")
    assert normalize_model_visual_references(
        text, required_source_text="@[吉迪恩]转头。",
        registered_names=("吉迪恩", "吉迪恩/视角-正面特写"),
    ) == text
    plain = body("吉迪恩转头。")
    assert normalize_model_visual_references(
        plain, required_source_text="@[吉迪恩/束缚形态]转头。",
        registered_names=("吉迪恩", "吉迪恩/束缚形态"),
    ) == plain


def test_unregistered_names_and_ascii_substrings_stay_unbound():
    text = body("Anna turns. Ann waits. 新人物转头。")
    fixed = normalize_model_visual_references(
        text, required_source_text="@[Ann] @[新人物]",
        registered_names=("Ann",),
    )
    assert "Anna turns. @[Ann] waits." in fixed
    assert extract_mentions(fixed) == ["石殿", "Ann"]


def test_repair_visual_error_on_same_line_as_dialogue():
    text = "@[埃利亚斯>伏低；@[暗影锁链]连接两人。@[吉迪恩]{Don't look up. @[埃利亚斯>}"
    fixed, repairs = repair_model_mention_closers(text, registered_names=("埃利亚斯", "暗影锁链", "吉迪恩"))
    assert fixed == text.replace("@[埃利亚斯>伏低", "@[埃利亚斯]伏低")
    assert repairs == [(0, "埃利亚斯")]
    assert extract_mentions(fixed) == ["埃利亚斯", "暗影锁链"]
