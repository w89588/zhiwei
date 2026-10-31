"""Portable chat exports. Message content is always literal, never active Markdown."""
import html
import json
import re
from datetime import datetime, timezone, timedelta

SHANGHAI = timezone(timedelta(hours=8))


def markdown_text(value):
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', html.escape(str(value))).replace('\n', ' ')


def fenced(value, language='text'):
    value = str(value)
    longest = max((len(m.group()) for m in re.finditer(r'`+', value)), default=0)
    fence = '`' * max(3, longest + 1)
    return f'{fence}{language}\n{value}\n{fence}\n'


def markdown(items, user, name=''):
    lines = [f'# {markdown_text(name or user)} · 微信聊天记录\n',
             f'会话 ID：{markdown_text(user)}  \n消息数：{len(items)}  \n时间：北京时间（UTC+08:00）\n']
    day = ''
    for item in items:
        stamp = datetime.fromtimestamp(item['timestamp'], SHANGHAI)
        if stamp.date().isoformat() != day:
            day = stamp.date().isoformat()
            lines.append(f'## {day}\n')
        lines.append(f"### {stamp:%H:%M:%S} · {markdown_text(item['sender'])} · {markdown_text(item['type_name'])}\n")
        lines.append(f"消息 ID：{markdown_text(item['id'])}  \n发送者 ID：{markdown_text(item.get('sender_id', ''))}\n")
        lines.append(fenced(item.get('text', '')))
        if item.get('extra'):
            lines.extend(['**结构化信息**\n', fenced(json.dumps(item['extra'], ensure_ascii=False, indent=2), 'json')])
        if item.get('raw') and item['raw'] != item.get('text'):
            lines.extend(['**原始内容**\n', fenced(item['raw'])])
    if not items:
        lines.append('所选范围没有聊天记录。\n')
    return '\n'.join(lines)
