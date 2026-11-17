import json

import pytest

from lib.backends.text_backends.base import resolve_schema
from lib.backends.text_backends.openai_policy import supports_strict_schema
from lib.infra.text_utils import DuplicateJSONKeyError
from lib.prompts.prompt_builders_reference import render_authoring_context
from lib.script.reference_video.authoring_response import (
    authoring_response_model,
    parse_authoring_json,
    recover_repeated_unit_fields,
)


def document(action):
    return f"【本组设定】\n场景。\n【开场承接】\n人在左。\n【分段时间轴】\n【0-4s】\n{action}\n【组尾交接】\n人在右。\n【必要限制】\n无。"


def test_batch_schema_is_closed_and_exact():
    schema = resolve_schema(authoring_response_model(3))
    assert supports_strict_schema(schema)
    assert schema['properties']['units']['minItems'] == 3
    assert schema['properties']['units']['maxItems'] == 3
    with pytest.raises(ValueError, match="units"):
        authoring_response_model(3).model_validate({'title': 't', 'units': [{'text': 'a'}]})


def test_recover_complete_duplicate_documents_without_losing_any_text():
    sources = [{'text': '@[甲]入场。@[甲]{一。}'}, {'text': '@[乙]坐下。@[乙]{二。}'}]
    texts = [document(s['text']) for s in sources]
    raw = '{"title":"t","units":[{' + ','.join('"text":' + json.dumps(t, ensure_ascii=False) for t in texts) + '}]}'
    with pytest.raises(DuplicateJSONKeyError):
        parse_authoring_json(raw)
    recovered = recover_repeated_unit_fields(raw, sources)
    assert json.loads(recovered)['units'] == [{'text': t} for t in texts]
    assert recover_repeated_unit_fields(raw, sources[::-1]) is None
    assert recover_repeated_unit_fields(raw, sources[:1]) is None
    assert recover_repeated_unit_fields(raw.replace('"title":"t"', '"title":"t","title":"x"'), sources) is None
    assert recover_repeated_unit_fields(raw.replace('【组尾交接】', '缺失标题'), sources) is None


def test_context_keeps_manual_prose_and_source_contract_without_repeated_camera_expansion():
    context = [
        {'unit_id': '1', 'text': document('摄影展开' * 100), '_authoring_source_text': '原事件对白'},
        {'unit_id': '2', 'text': '目标正文'},
        {'unit_id': '3', 'text': '其他事件', 'generation_prompt': '人工特殊镜头'},
    ]
    rendered = render_authoring_context(context, [context[1]])
    assert '原事件对白' in rendered
    assert '人在右。' in rendered
    assert '摄影展开' not in rendered
    assert '目标正文' not in rendered
    assert '人工特殊镜头' in rendered
    assert context[1]['text'] == '目标正文'
