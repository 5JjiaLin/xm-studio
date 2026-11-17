import pytest

from lib.script.reference_video.authoring_envelope import unpack_authoring_units
from lib.script.script_models import ReferencePromptAuthoringFlatScript


def document(n):
    return f"【本组设定】\n第{n}组\n【开场承接】\n起点\n【分段时间轴】\n【0-6s】\n动作{n}\n【组尾交接】\n终点\n【必要限制】\n无"


def flat(*texts):
    return ReferencePromptAuthoringFlatScript(title="原标题", units=[{"text": t} for t in texts])


def test_unpack_complete_documents_preserves_text_and_order():
    originals = [document(i) for i in range(3)]
    result = unpack_authoring_units(flat("\n".join(originals)), 3)
    assert result.title == "原标题"
    assert [u.text for u in result.units] == originals


@pytest.mark.parametrize(("texts", "count"), [
    ([document(1)], 3),
    ([document(1) + "\n" + document(2)], 3),
    (["模型说明\n" + document(1) + "\n" + document(2)], 2),
    ([document(1) + "\n" + document(2).replace("【开场承接】", "")], 2),
    ([document(1) + "\n" + document(2).replace("【必要限制】", "【组尾交接】")], 2),
    ([document(1), document(2)], 1),
])
def test_ambiguous_or_incomplete_response_is_not_guessed(texts, count):
    original = flat(*texts)
    assert unpack_authoring_units(original, count) is original
