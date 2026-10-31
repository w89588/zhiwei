import json
from pathlib import Path

import pytest
import tomlkit

from helper import client_setup as setup


@pytest.fixture
def registry(tmp_path, monkeypatch):
    specs = [
        setup.Client('codex', 'Codex', tmp_path / 'codex/config.toml', (), bucket='mcp_servers', skill=tmp_path / 'skills/wechat-chat/SKILL.md'),
        setup.Client('cursor', 'Cursor', tmp_path / 'cursor/mcp.json', (), typed=True),
        setup.Client('vscode', 'VS Code', tmp_path / 'code/mcp.json', (), bucket='servers', typed=True),
        setup.Client('claude-code', 'Claude Code', tmp_path / '.claude.json', (), typed=True),
    ]
    for c in specs:
        c.path.parent.mkdir(parents=True, exist_ok=True)
        c.path.write_text('' if c.bucket == 'mcp_servers' else '{}', encoding='utf-8')
    monkeypatch.setattr(setup, 'clients', lambda: specs)
    setup.PLANS.clear()
    return specs, {'command': 'C:\\App With Spaces\\WechatHelperMCP.exe', 'args': [], 'env': {'WECHAT_HELPER_DATA': str(tmp_path / 'data')}}


def test_toml_merge_backup_skill_and_idempotence(registry):
    specs, server = registry
    codex = specs[0]
    original = b'# Keep this comment\r\nmodel = "test"\r\n[mcp_servers.other]\r\ncommand = "existing.exe" # keep\r\n'
    codex.path.write_bytes(original)
    plan = setup.preview(['codex'], server)
    assert codex.path.read_bytes() == original
    assert all('existing.exe' not in json.dumps(item) for item in plan['items'])
    result = setup.apply(plan['token'], server)['items'][0]
    assert result['ok']
    assert Path(result['backups'][0]).read_bytes() == original
    merged = codex.path.read_text('utf-8')
    assert '# Keep this comment' in merged and '# keep' in merged
    doc = tomlkit.parse(merged)
    assert doc['model'] == 'test' and doc['mcp_servers']['other']['command'] == 'existing.exe'
    assert doc['mcp_servers']['wechat-helper'] == server
    assert codex.skill.read_bytes() == setup.SKILL.read_bytes()
    again = setup.apply(setup.preview(['codex'], server)['token'], server)['items'][0]
    assert again['changed'] is False and again['backups'] == []


@pytest.mark.parametrize('text', [
    '{"theme":"dark","mcpServers":{"other":{"command":"other","env":{"SECRET":"keep"}}}}',
    '{\n // keep comment\n "servers": {"other": {"command": "other"},},\n "unrelated": ",}", // literal comma\n}',
    '{"servers": {/* keep */}}',
    '{ // comment\n "theme": "dark", /* retain */\n}',
])
def test_jsonc_preserves_unrelated_content_and_comments(registry, text):
    specs, server = registry
    client = specs[2] if 'servers' in text or 'mcpServers' not in text else specs[1]
    original = b'\xef\xbb\xbf' + text.encode()
    client.path.write_bytes(original)
    result = setup.apply(setup.preview([client.id], server, False)['token'], server)['items'][0]
    assert result['ok']
    raw = client.path.read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf')
    merged = raw.decode('utf-8-sig')
    old = json.loads(setup._json_clean(text))
    new = json.loads(setup._json_clean(merged))
    old.setdefault(client.bucket, {})['wechat-helper'] = setup._desired(client, server)
    assert old == new
    for comment in ['// keep comment', '// literal comma', '/* keep */', '// comment', '/* retain */']:
        if comment in text:
            assert comment in merged
    assert Path(result['backups'][0]).read_bytes() == original


def test_conflict_is_explicit_and_secret_is_not_exposed(registry):
    specs, server = registry
    client = specs[1]
    client.path.write_text('{"mcpServers":{"wechat-helper":{"command":"custom","env":{"SECRET":"hidden"}},"other":{"command":"keep"}}}')
    assert setup.discover(server)['items'][1]['state'] == 'conflict'
    assert 'hidden' not in json.dumps(setup.discover(server))
    with pytest.raises(ValueError, match='同名'):
        setup.preview([client.id], server)
    plan = setup.preview([client.id], server, replace_existing=True)
    assert plan['items'][0]['conflict'] and 'hidden' not in json.dumps(plan)
    assert setup.apply(plan['token'], server)['items'][0]['ok']
    assert json.loads(client.path.read_text())['mcpServers']['other']['command'] == 'keep'


def test_stale_plan_writes_nothing_and_cannot_replay(registry):
    specs, server = registry
    plan = setup.preview(['codex', 'cursor'], server)
    specs[1].path.write_text('{"changed":true}')
    with pytest.raises(ValueError, match='修改'):
        setup.apply(plan['token'], server)
    assert specs[0].path.read_bytes() == b'' and not specs[0].skill.exists()
    with pytest.raises(ValueError, match='过期'):
        setup.apply(plan['token'], server)


@pytest.mark.parametrize('text', ['{"mcpServers":[]}', '{"x":1,"x":2}', '{"SECRET":"hidden"', '{/* unclosed'])
def test_invalid_file_remains_untouched(registry, text):
    specs, server = registry
    specs[1].path.write_text(text)
    with pytest.raises(ValueError):
        setup.preview(['cursor'], server)
    assert specs[1].path.read_text() == text
    assert setup.discover(server)['items'][1]['state'] == 'invalid'
    assert 'hidden' not in json.dumps(setup.discover(server))


def test_custom_skill_preserved_until_explicit_replacement(registry):
    specs, server = registry
    skill = specs[0].skill
    skill.parent.mkdir(parents=True)
    skill.write_text('custom skill')
    with pytest.raises(ValueError, match='同名'):
        setup.preview(['codex'], server)
    assert skill.read_text() == 'custom skill'
    result = setup.apply(setup.preview(['codex'], server, replace_existing=True)['token'], server)['items'][0]
    assert Path(result['backups'][1]).read_text() == 'custom skill'


def test_whitelist_and_undetected_clients(registry, monkeypatch):
    specs, server = registry
    with pytest.raises(ValueError):
        setup.preview(['../../arbitrary'], server)
    specs[1].path.unlink()
    assert not setup.discover(server)['items'][1]['detected']
    with pytest.raises(ValueError, match='未检测到'):
        setup.preview(['cursor'], server)


def test_partial_write_reports_actual_paths(registry, monkeypatch):
    specs, server = registry
    writer = setup._write
    def fail_skill(path, old, new):
        if path == specs[0].skill:
            raise OSError('do not expose source credentials')
        return writer(path, old, new)
    monkeypatch.setattr(setup, '_write', fail_skill)
    result = setup.apply(setup.preview(['codex'], server)['token'], server)['items'][0]
    assert result['ok'] is False and result['written'] == [str(specs[0].path)]
    assert result['backups'] and 'credentials' not in json.dumps(result)


def test_existing_skill_with_different_folder_name_is_reused(registry):
    specs, server = registry
    default = specs[0].skill
    installed = default.parent.parent / 'older-folder/SKILL.md'
    installed.parent.mkdir(parents=True)
    installed.write_text('---\nname: wechat-chat\ndescription: Older installation\n---\nCustom instructions')
    with pytest.raises(ValueError, match='同名'):
        setup.preview(['codex'], server)
    plan = setup.preview(['codex'], server, replace_existing=True)
    assert plan['items'][0]['files'][1]['path'] == str(installed)
    assert setup.apply(plan['token'], server)['items'][0]['ok']
    assert installed.read_bytes() == setup.SKILL.read_bytes() and not default.exists()
