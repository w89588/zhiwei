"""Render generated memory as readable Markdown while keeping provenance out of the body."""
import json
import re

_ID = re.compile(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])', re.I)
_ALIAS = re.compile(r'(?<![A-Za-z0-9])E\d+(?![A-Za-z0-9])')
_OMIT_KEYS = {'id', 'evidence', 'evidence_id', 'evidence_ids', 'evidenceid', 'evidence_refs',
    'source', 'source_type', 'source_sessions', '证据', '证据id', '证据编号', '证据索引', '消息id',
    '来源', '来源类型', '消息来源', '证据来源', '引用', '整理时间', '生成时间', '分析时间'}
_FENCE = re.compile(r'```([^\n]*)\n(.*?)```', re.S)


def _clean_text(value):
    value = re.sub(r'(?m)^\s*#{1,6}\s*有证据的增量记录\s*$', '', str(value))
    value = re.sub(r'(?m)^(\s*[-*]\s*)\d{4}-\d{2}-\d{2}\s*[（(](?:新增事实|历史补录)[）)]\s*[:：]\s*', r'\1', value)
    value = re.sub(r'\[\s*(?:证据(?:\s*ID)?\s*[:：]\s*)?(?:[0-9a-f]{64}|E\d+)(?:\s*[,，、]\s*(?:[0-9a-f]{64}|E\d+))*\s*\]', '', str(value), flags=re.I)
    value = _ID.sub('', value)
    value = _ALIAS.sub('', value)
    value = re.sub(r'(?im)^\s*[-*]?\s*(?:来源)?证据(?:\s*ID|编号|索引)?\s*[:：]\s*[,，、\s]*$', '', value)
    value = re.sub(r'[ \t]+\n', '\n', value)
    value = re.sub(r'\n{3,}', '\n\n', value).strip()
    lines, seen = [], set()
    for line in value.splitlines():
        if line.lstrip().startswith(('- ', '* ')):
            key = line.strip()[2:].strip()
            if key and key in seen:
                continue
            seen.add(key)
        lines.append(line)
    return '\n'.join(lines).strip()


def _render(value, level=3):
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(('{', '[')):
            try:
                return _render(json.loads(stripped), level)
            except (ValueError, TypeError):
                pass
        return _clean_text(value)
    if value is None:
        return ''
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        pieces = []
        for item in value:
            text = _render(item, min(level+1, 6))
            if text:
                pieces.append(text if isinstance(item, (dict, list)) else '- '+text.replace('\n', '\n  '))
        return '\n\n'.join(pieces)
    if isinstance(value, dict):
        pieces = []
        for key, item in value.items():
            if str(key).strip().lower().replace('_', '') in {k.replace('_', '') for k in _OMIT_KEYS}:
                continue
            label = _clean_text(key)
            text = _render(item, min(level+1, 6))
            if not text:
                continue
            if isinstance(item, (dict, list)):
                pieces.append('#'*min(level,6)+' '+label+'\n\n'+text)
            else:
                pieces.append('**'+label+'**：'+text)
        return '\n\n'.join(pieces)
    return ''


def clean_memory_markdown(text):
    """Turn structured model output into Markdown; never store JSON fences or IDs."""
    if not isinstance(text, str):
        raise ValueError('记忆正文格式无效')
    stripped = text.strip()
    if stripped.startswith(('{', '[')):
        try:
            return _clean_text(_render(json.loads(stripped)))
        except (ValueError, TypeError):
            pass
    def convert(match):
        language, body = match.group(1).strip().lower(), match.group(2).strip()
        if language == 'json' or body.startswith(('{', '[')):
            try:
                return _render(json.loads(body))
            except (ValueError, TypeError):
                raise ValueError('模型返回的 JSON 记忆格式不完整，已停止写入') from None
        return _clean_text(body)
    text = _FENCE.sub(convert, text)
    # Convert valid inline JSON objects too, without interpreting ordinary Markdown links.
    decoder, pieces, pos = json.JSONDecoder(), [], 0
    while pos < len(text):
        start = text.find('{', pos)
        if start < 0:
            pieces.append(text[pos:]); break
        pieces.append(text[pos:start])
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            pieces.append('{'); pos = start+1; continue
        pieces.append('\n\n'+_render(value)+'\n\n'); pos = end
    result = _clean_text(''.join(pieces))
    if not result:
        raise ValueError('转换后记忆正文为空，已停止写入')
    return result
