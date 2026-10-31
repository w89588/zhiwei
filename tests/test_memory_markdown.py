import json
import pytest
from backend.memory_markdown import clean_memory_markdown


def test_json_summary_becomes_markdown_without_ids_or_json_fence():
    evidence = 'a'*64
    body = {'整理时间':'2026-09-26','核心话题':[{'主题':'讨论项目','内容':'计划下周见面','证据ID':evidence}],
        '约定与待办':['准备材料'],'关系变化':['保持联系'], '未知/存疑':[]}
    result = clean_memory_markdown('# 每日记忆\n\n```json\n'+json.dumps(body,ensure_ascii=False)+'\n```\n\n['+evidence+']')
    assert result.startswith('# 每日记忆') and '### 核心话题' in result
    assert '讨论项目' in result and '准备材料' in result
    assert '```' not in result and evidence not in result and '证据ID' not in result
    assert '"核心话题"' not in result and '整理时间' not in result


def test_inline_json_and_legacy_history_markers_are_cleaned():
    evidence='b'*64
    text = '# 画像\n\n## 有证据的增量记录\n\n- 2026-09-25（历史补录）：喜欢散步 [证据: '+evidence+']\n\n- 2026-09-25（新增事实）：喜欢散步 [证据: '+evidence+']\n\n补充：{"偏好":"听音乐"}'
    result=clean_memory_markdown(text)
    assert result.count('喜欢散步') == 1 and '听音乐' in result
    assert '历史补录' not in result and '新增事实' not in result and evidence not in result
    assert '2026-09-25' not in result and '{' not in result


def test_invalid_json_fence_is_not_saved_as_memory():
    with pytest.raises(ValueError, match='JSON'):
        clean_memory_markdown('# 记忆\n```json\n{"a":\n```')
