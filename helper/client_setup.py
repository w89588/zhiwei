"""Local AI client discovery and reviewed, additive MCP/SKILL setup.

No client settings or credentials are returned. Plans are short-lived and bound
to the exact bytes reviewed; only registered client locations can be written.
"""
import json
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from .integration import SKILL

NAME = 'wechat-helper'
LOCK = threading.RLock()
PLANS = {}
MAX_SIZE = 8 * 1024 * 1024


@dataclass(frozen=True)
class Client:
    id: str
    name: str
    path: Path
    markers: tuple
    cli: str = ''
    bucket: str = 'mcpServers'
    skill: Path | None = None
    typed: bool = False
    note: str = '保存后重启客户端，并在客户端内允许使用 wechat-helper。'


def _skill_location(default):
    """Reuse a uniquely named older installation, avoiding duplicate skills."""
    if default.exists():
        return default
    matches = []
    try:
        for path in default.parent.parent.glob('*/SKILL.md'):
            if path.stat().st_size <= 64 * 1024:
                try:
                    header = path.read_text('utf-8-sig').split('---', 2)
                except UnicodeError:
                    continue
                if len(header) == 3 and re.search(r'^name:\s*[\"\']?wechat-chat[\"\']?\s*$', header[1], re.M):
                    matches.append(path)
    except OSError:
        return default
    if len(matches) > 1:
        raise ValueError('发现多个 wechat-chat 技能，请在客户端中整理同名技能后重试')
    return matches[0] if matches else default


def clients():
    home = Path.home()
    roaming = Path(os.environ.get('APPDATA', str(home / 'AppData/Roaming')))
    local = Path(os.environ.get('LOCALAPPDATA', str(home / 'AppData/Local')))
    codex = Path(os.environ.get('CODEX_HOME', str(home / '.codex')))
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(home / '.claude')))
    # Claude Code keeps the legacy user file alongside its config directory.
    claude_json = claude / '.claude.json' if 'CLAUDE_CONFIG_DIR' in os.environ else home / '.claude.json'
    return [
        Client('codex', 'Codex', codex / 'config.toml', (codex, local / 'Programs/Codex'),
               'codex', 'mcp_servers', home / '.agents/skills/wechat-chat/SKILL.md'),
        Client('claude-desktop', 'Claude Desktop', roaming / 'Claude/claude_desktop_config.json',
               (roaming / 'Claude', local / 'AnthropicClaude'), note='完全退出 Claude Desktop 后重新打开，检查开发者设置中的 MCP。SKILL 可另行下载。'),
        Client('claude-code', 'Claude Code', claude_json, (claude,), 'claude',
               skill=claude / 'skills/wechat-chat/SKILL.md', typed=True),
        Client('cursor', 'Cursor', home / '.cursor/mcp.json', (home / '.cursor', local / 'Programs/cursor'),
               'cursor', skill=home / '.cursor/skills/wechat-chat/SKILL.md', typed=True),
        Client('windsurf', 'Windsurf', home / '.codeium/windsurf/mcp_config.json',
               (home / '.codeium/windsurf', roaming / 'Windsurf', local / 'Programs/Windsurf'), 'windsurf'),
        Client('devin', 'Devin Desktop', roaming / 'devin/mcp_config.json',
               (roaming / 'devin', local / 'Programs/Devin'), 'devin',
               note='此配置用于 Cascade；Devin Local 请使用客户端内的 MCP 设置。'),
        Client('vscode', 'VS Code', roaming / 'Code/User/mcp.json',
               (roaming / 'Code', local / 'Programs/Microsoft VS Code'), 'code', 'servers', typed=True,
               note='写入默认用户配置。其他配置档案、远程窗口请在 VS Code 中另行设置；保存后重新加载窗口并信任此 MCP。'),
    ]


def _read(path):
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_SIZE:
        raise ValueError('配置文件过大或不是普通文件，请在客户端内手动配置')
    return path.read_bytes()


def _json_clean(text):
    """Mask JSONC comments/trailing commas without moving any source offsets."""
    chars = list(text)
    i = 0
    while i < len(chars):
        if text[i] == '"':
            i += 1
            while i < len(chars):
                if text[i] == '\\':
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
        elif text.startswith('//', i) or text.startswith('/*', i):
            start = i
            if text.startswith('//', i):
                end = text.find('\n', i)
                i = len(chars) if end < 0 else end
            else:
                end = text.find('*/', i + 2)
                if end < 0:
                    raise ValueError('配置中的注释未闭合')
                i = end + 2
            for j in range(start, i):
                if chars[j] not in '\r\n':
                    chars[j] = ' '
        else:
            i += 1
    clean = ''.join(chars)
    # String tokens must be skipped: a literal ",}" is not a trailing comma.
    for match in re.finditer(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])', clean):
        if match.group() == ',':
            chars[match.start()] = ' '
    return ''.join(chars)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('配置包含重复字段，请先在客户端内修正')
        result[key] = value
    return result


def _parse(client, raw):
    text = (raw or b'').decode('utf-8-sig')
    try:
        document = tomlkit.parse(text) if client.bucket == 'mcp_servers' else json.loads(_json_clean(text or '{}'), object_pairs_hook=_pairs)
    except (ValueError, tomlkit.exceptions.ParseError):
        raise ValueError('配置格式无效，请在客户端内修正后重新检测') from None
    if not isinstance(document, dict):
        raise ValueError('配置根节点必须是对象')
    bucket = document.get(client.bucket, {})
    if not isinstance(bucket, dict):
        raise ValueError('MCP 配置节点必须是对象')
    return text, document, bucket.get(NAME)


def _members(clean, start):
    decoder = json.JSONDecoder()
    pos = start + 1
    members = {}
    while True:
        pos = re.compile(r'\s*').match(clean, pos).end()
        if clean[pos] == '}':
            return members, pos
        key, pos = decoder.raw_decode(clean, pos)
        pos = re.compile(r'\s*').match(clean, pos).end() + 1  # colon
        pos = re.compile(r'\s*').match(clean, pos).end()
        value_start = pos
        _, pos = decoder.raw_decode(clean, pos)
        members[key] = (value_start, pos)
        pos = re.compile(r'\s*').match(clean, pos).end()
        if clean[pos] == ',':
            pos += 1


def _json_set(text, keys, value):
    text = text or '{}\n'
    clean = _json_clean(text)
    start = len(clean) - len(clean.lstrip())
    for index, key in enumerate(keys):
        members, closing = _members(clean, start)
        if key in members:
            begin, end = members[key]
            if index + 1 < len(keys):
                start = begin
                continue
            indentation = re.match(r'[ \t]*', text[text.rfind('\n', 0, begin) + 1:]).group()
            rendered = json.dumps(value, ensure_ascii=False, indent=2).replace('\n', '\n' + indentation)
            return text[:begin] + rendered + text[end:]
        nested = value
        for child in reversed(keys[index + 1:]):
            nested = {child: nested}
        close_indent = re.match(r'[ \t]*', text[text.rfind('\n', 0, closing) + 1:]).group()
        indent = close_indent + '  '
        rendered = json.dumps(nested, ensure_ascii=False, indent=2).replace('\n', '\n' + indent)
        newline = '\r\n' if '\r\n' in text else '\n'
        insertion = newline + indent + json.dumps(key) + ': ' + rendered.replace('\n', newline) + newline + close_indent
        # Preserve a pre-existing trailing comma, if present.
        if members:
            last_end = list(members.values())[-1][1]
            tail = _json_clean('[' + text[last_end:closing] + '0]')
            if ',' not in tail:
                text = text[:last_end] + ',' + text[last_end:]
                closing += 1
        return text[:closing] + insertion + text[closing:]
    raise ValueError('无效配置路径')


def _desired(client, server):
    return {**({'type': 'stdio'} if client.typed else {}), **server}


def _merge(client, raw, server):
    text, doc, existing = _parse(client, raw)
    if existing == server:
        return raw
    if client.bucket == 'mcp_servers':
        if client.bucket not in doc:
            doc[client.bucket] = tomlkit.table()
        doc[client.bucket][NAME] = server
        result = tomlkit.dumps(doc)
    else:
        result = _json_set(text, [client.bucket, NAME], server)
    result_raw = result.encode('utf-8')
    if raw and raw.startswith(b'\xef\xbb\xbf'):
        result_raw = b'\xef\xbb\xbf' + result_raw
    if _parse(client, result_raw)[2] != server:
        raise ValueError('配置合并校验失败，未写入文件')
    return result_raw


def _detected(client):
    if client.path.exists():
        return '发现配置文件'
    if any(marker.is_dir() for marker in client.markers):
        return '发现应用或配置目录'
    if client.cli and shutil.which(client.cli):
        return '发现命令行入口'
    if client.id == 'codex':
        local = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local')))
        try:
            if any((local / 'Packages').glob('*Codex*')):
                return '发现 Windows 应用包目录'
        except OSError:
            pass
    return ''


def discover(server):
    items = []
    for client in clients():
        evidence = _detected(client)
        error = ''
        state = 'missing'
        try:
            existing = _parse(client, _read(client.path))[2]
            if existing is not None:
                state = 'configured' if existing == _desired(client, server) else 'conflict'
        except (OSError, UnicodeError, ValueError):
            error = '配置不可读取或格式无效，请先在客户端内修正'
            state = 'invalid'
        items.append({'id': client.id, 'name': client.name, 'detected': bool(evidence),
                      'evidence': evidence, 'state': state, 'error': error,
                      'path': str(client.path), 'skill_path': str(client.skill) if client.skill else '',
                      'note': client.note})
    return {'items': items}


def preview(ids, server, install_skill=True, replace_existing=False):
    if not ids or len(ids) > 10 or len(ids) != len(set(ids)):
        raise ValueError('请选择需要配置的客户端')
    registry = {client.id: client for client in clients()}
    plans = []
    for client_id in ids:
        client = registry.get(client_id)
        if not client or not _detected(client):
            raise ValueError('客户端未检测到，请使用手动配置')
        raw = _read(client.path)
        existing = _parse(client, raw)[2]
        desired = _desired(client, server)
        conflict = existing is not None and existing != desired
        files = [(client.path, raw, _merge(client, raw, desired), 'MCP')]
        if install_skill and client.skill:
            skill = _skill_location(client.skill)
            old_skill = _read(skill)
            new_skill = SKILL.read_bytes()
            conflict |= old_skill is not None and old_skill != new_skill
            files.append((skill, old_skill, new_skill, 'SKILL'))
        if conflict and not replace_existing:
            raise ValueError(f'{client.name} 已有不同的同名配置或 SKILL；请勾选“替换同名内容”后重新预览')
        plans.append({'client': client, 'files': files, 'conflict': conflict,
                      'config': {client.bucket: {NAME: desired}}})
    token = secrets.token_urlsafe(24)
    with LOCK:
        now = time.monotonic()
        for key in list(PLANS):
            if PLANS[key]['expires'] < now:
                del PLANS[key]
        if len(PLANS) >= 32:
            del PLANS[next(iter(PLANS))]
        PLANS[token] = {'expires': now + 600, 'plans': plans, 'server': server}
    return {'token': token, 'items': [
        {'id': p['client'].id, 'name': p['client'].name, 'note': p['client'].note,
         'conflict': p['conflict'], 'config': p['config'],
         'files': [{'path': str(path), 'kind': kind, 'action': 'unchanged' if old == new else 'update' if old is not None else 'create'}
                   for path, old, new, kind in p['files']]}
        for p in plans]}


def _write(path, old, new):
    if _read(path) != old:
        raise ValueError('预览后文件已被修改，请重新检测并预览')
    if old == new:
        return ''
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4)
    backup = path.with_name(path.name + '.wechat-helper-' + suffix + '.bak') if old is not None else None
    if backup:
        # Copy next to the user's settings; never publish contents through the API.
        shutil.copy2(path, backup)
        if backup.read_bytes() != old:
            raise ValueError('备份期间文件发生变化，请重新预览')
    temp = path.with_name(path.name + '.wechat-helper-' + suffix + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(new)
            stream.flush()
            os.fsync(stream.fileno())
        if old is not None:
            shutil.copymode(path, temp)
        if _read(path) != old:
            raise ValueError('写入前文件已发生变化，请重新预览')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return str(backup) if backup else ''


def apply(token, server):
    with LOCK:
        plan = PLANS.pop(token, None)
        if not plan or plan['expires'] < time.monotonic() or plan['server'] != server:
            raise ValueError('预览已过期，请重新预览')
        # Reject stale plans before writing any of the selected clients.
        for item in plan['plans']:
            for path, old, new, kind in item['files']:
                if _read(path) != old:
                    raise ValueError('预览后文件已被修改，请重新检测并预览')
        results = []
        for item in plan['plans']:
            backups = []
            written = []
            try:
                for path, old, new, kind in item['files']:
                    backup = _write(path, old, new)
                    if backup:
                        backups.append(backup)
                    if old != new:
                        written.append(str(path))
                results.append({'id': item['client'].id, 'name': item['client'].name,
                                'ok': True, 'changed': bool(written), 'written': written,
                                'backups': backups, 'note': item['client'].note})
            except (OSError, ValueError):
                results.append({'id': item['client'].id, 'name': item['client'].name,
                                'ok': False, 'written': written, 'backups': backups,
                                'note': '部分文件可能已写入；请检查已写入路径。文件不可写或发生变化，请重新检测。'})
        return {'items': results, 'connection_verified': False}
