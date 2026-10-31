import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from helper import exporting, storage
from helper.integration import Integration
from helper.wechat import Wechat


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    from helper import server
    monkeypatch.setattr(storage, 'DATA', tmp_path)
    monkeypatch.setattr('helper.wechat.DATA', tmp_path)
    monkeypatch.setattr('helper.integration.DATA', tmp_path)
    w = Wechat()
    w.root = tmp_path / 'wxid_me_abcd' / 'db_storage'
    w.names = {'friend': '朋友 <甲>', 'wxid_me': '本人'}
    w.status.update(state='ready', generation=1)
    table = 'Msg_' + hashlib.md5(b'friend').hexdigest()
    for shard in (0, 1):
        path = tmp_path / f'{shard}.db'
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE Name2Id(user_name TEXT)')
            con.executemany('INSERT INTO Name2Id VALUES (?)', [('wxid_me',), ('friend',)])
            con.execute(f'CREATE TABLE [{table}](local_id INTEGER,real_sender_id INTEGER,local_type INTEGER,create_time INTEGER,message_content TEXT)')
            for i in range(3):
                con.execute(f'INSERT INTO [{table}] VALUES (?,?,?,?,?)', (i, 1 if i % 2 else 2, 1, 1728000000+2*i+shard, f'消息{i}分片{shard}\n```\n<script>alert(1)</script>'))
        rel = f'message/message_{shard}.db'
        w.cache[rel] = (None, path)
        w.tables[rel] = {table, 'Name2Id'}
    access = Integration()
    w.update_workspace('friend', True)
    w.status['generation'] = 1
    monkeypatch.setattr(server, 'wechat', w)
    monkeypatch.setattr(server, 'integration', access)
    return TestClient(server.app), server, access, w, tmp_path


def test_markdown_literal_content_and_metadata():
    item = {'id': 'db:1', 'timestamp': 1728000000, 'sender': '甲 <img> *x*',
            'sender_id': 'friend', 'type_name': '链接 / 文件', 'text': '多行\n````\n# 标题\n<script>data</script>',
            'raw': '<xml><title>附件</title></xml>', 'extra': {'filename': '文档.md', 'refermsg/content': '原文'}}
    result = exporting.markdown([item], 'friend', '测试')
    assert '2024-10-04' in result and '08:00:00' in result
    assert '`````text\n'+item['text']+'\n`````' in result
    assert '<img>' not in result and '&lt;img&gt;' in result
    assert item['raw'] in result and '文档.md' in result and '原文' in result
    assert '所选范围没有聊天记录' in exporting.markdown([], 'friend')


def test_client_configuration_is_ui_only_and_does_not_enable_sharing(desktop, monkeypatch):
    from helper import client_setup
    client, server, access, _, tmp_path = desktop
    path = tmp_path / 'ai-client/config.toml'
    path.parent.mkdir()
    path.write_text('model = "keep"\n')
    monkeypatch.setattr(client_setup, 'clients', lambda: [client_setup.Client('codex', 'Codex', path, (), bucket='mcp_servers')])
    ui = {'X-Helper-Token': server.TOKEN}
    external = {'X-Wechat-Integration-Token': access.token}
    for headers in [{}, external]:
        assert client.get('/api/integration/clients', headers=headers).status_code == 401
        assert client.post('/api/integration/clients/preview', json={'clients':['codex']}, headers=headers).status_code == 401
        assert client.post('/api/integration/clients/apply', json={'token':'unknown'}, headers=headers).status_code == 401
    preview = client.post('/api/integration/clients/preview', json={'clients':['codex']}, headers=ui)
    assert preview.status_code == 200, preview.text
    result = client.post('/api/integration/clients/apply', json={'token':preview.json()['token']}, headers=ui)
    assert result.status_code == 200 and result.json()['items'][0]['ok']
    assert access.enabled is False and not (tmp_path/'integration-connection.bin').exists()
    assert client.get('/api/integration/clients', headers=ui).json()['items'][0]['state'] == 'configured'
    assert 'model = "keep"' in path.read_text()
    assert client.post('/api/integration/clients/apply', json={'token':preview.json()['token']}, headers=ui).status_code == 409


def test_export_formats_and_filtering(desktop):
    client, server, access, w, _ = desktop
    headers = {'X-Helper-Token': server.TOKEN}
    full = client.get('/api/export', params={'user': 'friend', 'format': 'markdown'}, headers=headers)
    assert full.status_code == 200 and full.headers['content-type'].startswith('text/markdown')
    assert 'chat.md' in full.headers['content-disposition']
    assert full.text.count('### ') == 6 and '消息数：6' in full.text
    selected = client.get('/api/export', params={'user': 'friend', 'format': 'markdown', 'q': '消息1', 'start': 1728000002, 'end': 1728000002}, headers=headers)
    assert selected.text.count('### ') == 1 and '消息1分片0' in selected.text
    original = client.get('/api/export', params={'user': 'friend'}, headers=headers)
    assert len(original.json()) == 6
    assert client.get('/api/export', params={'user': 'friend', 'format': 'html'}, headers=headers).status_code == 422
    assert client.get('/api/export', params={'user': 'friend', 'start': 10, 'end': 1}, headers=headers).status_code == 422
    assert client.get('/api/integration/skill', headers=headers).text.startswith('---\nname: wechat-chat')


def test_opt_in_read_only_auth_and_revocation(desktop):
    client, server, access, _, tmp_path = desktop
    ui = {'X-Helper-Token': server.TOKEN}
    assert client.get('/api/external/status', headers=ui).status_code == 403
    assert client.post('/api/integration/settings', json={'enabled': True}).status_code == 401
    access.publish('http://127.0.0.1:8765')
    config = client.post('/api/integration/settings', json={'enabled': True}, headers=ui).json()
    assert config['enabled'] and 'mcpServers' in config['config']
    record = storage.read_json('integration-connection.bin', {}, secret=True)
    assert record['token'] == access.token and record['url'] == access.url
    assert access.token.encode() not in (tmp_path/'integration-connection.bin').read_bytes()
    external = {'X-Wechat-Integration-Token': access.token}
    assert client.get('/api/external/status', headers=ui).status_code == 401
    assert client.get('/api/status', headers=external).status_code == 401
    assert client.post('/api/connect', json={'path': 'ignored'}, headers=external).status_code == 401
    assert client.get('/api/external/status', headers=external).json()['state'] == 'ready'
    assert client.get('/api/external/status', headers={**external, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/external/status', headers={**external, 'Host': 'evil.example'}).status_code == 403
    assert client.post('/api/external/messages', json={}, headers=external).status_code == 405
    client.post('/api/integration/settings', json={'enabled': False}, headers=ui)
    assert not (tmp_path/'integration-connection.bin').exists()
    assert client.get('/api/external/status', headers=external).status_code == 403
    client.post('/api/integration/settings', json={'enabled': True}, headers=ui)
    assert access.token != external['X-Wechat-Integration-Token']
    assert client.get('/api/external/status', headers=external).status_code == 401


def test_external_paging_search_context_and_errors(desktop):
    client, _, access, w, _ = desktop
    access.configure(True)
    headers = {'X-Wechat-Integration-Token': access.token}
    sessions = client.get('/api/external/sessions', params={'q': '朋友'}, headers=headers).json()
    assert sessions['items'][0]['id'] == 'friend'
    pages = [client.get('/api/external/messages', params={'user': 'friend', 'offset': n, 'limit': 2}, headers=headers).json() for n in (0, 2, 4)]
    assert pages[0]['next_offset'] == 2 and pages[-1]['next_offset'] is None
    assert len({m['id'] for page in pages for m in page['items']}) == 6
    assert all(page['generation'] == 1 for page in pages)
    assert [m['timestamp'] for m in pages[0]['items']] == [1728000004, 1728000005]
    result = client.get('/api/external/messages', params={'user': 'friend', 'q': '消息1', 'format': 'markdown'}, headers=headers).json()
    assert result['total'] == 2 and result['markdown'].count('### ') == 2
    storage.write_json('profiles.json', {'friend': {'goals': '沟通目标'}})
    storage.write_json('summaries.json', {'friend': [{'text': '最近总结'}]})
    context = client.get('/api/external/context', params={'user': 'friend'}, headers=headers).json()
    assert context['profile']['goals'] == '沟通目标' and context['latest_summary']['text'] == '最近总结'
    for params in ({'user': 'friend', 'limit': 501}, {'user': 'friend', 'offset': -1}, {'user': 'friend', 'start': 2, 'end': 1}):
        assert client.get('/api/external/messages', params=params, headers=headers).status_code == 422
    w.status['state'] = 'idle'
    assert client.get('/api/external/messages', params={'user': 'friend'}, headers=headers).status_code == 409


def test_mcp_rejects_non_local_discovery(monkeypatch):
    from helper import mcp_server
    for url in ('https://example.com', 'http://evil.example:123', 'http://user@127.0.0.1:123', 'http://127.0.0.1:123/path'):
        monkeypatch.setattr(mcp_server, 'read_json', lambda *a, url=url, **kw: {'url': url, 'token': 'secret'})
        with pytest.raises(ValueError, match='本机接入地址无效'):
            mcp_server.wechat_status()


def test_workspace_scope_notes_and_immediate_removal(desktop, monkeypatch):
    client, server, access, w, _ = desktop
    ui = {'X-Helper-Token': server.TOKEN}
    access.configure(True)
    external = {'X-Wechat-Integration-Token': access.token}
    storage.write_json('workspace.json', {})
    assert client.get('/api/sessions', headers=ui).json() == []
    assert client.get('/api/external/sessions', headers=external).json()['total'] == 0
    assert client.get('/api/session-catalog', headers=external).status_code == 401
    catalog = client.get('/api/session-catalog', headers=ui).json()
    assert catalog['items'][0]['id'] == 'friend' and not catalog['items'][0]['selected']
    # A known ID must not bypass the list, even if its messages are in a shared snapshot.
    for path in ('messages', 'context'):
        assert client.get('/api/external/'+path, params={'user': 'friend'}, headers=external).status_code == 403
    for path in ('messages', 'profile', 'history', 'export', 'image'):
        params = {'user': 'friend', 'local_id': 1, 'database': 'message/message_0.db'}
        assert client.get('/api/'+path, params=params, headers=ui).status_code == 403
    assert client.post('/api/ai/run', json={'user': 'friend'}, headers=ui).status_code == 403
    assert client.post('/api/workspace/note', json={'user': 'friend', 'note': '未授权'}, headers=ui).status_code == 403
    assert client.post('/api/workspace/session', json={'user': 'guessed', 'selected': True}, headers=ui).status_code == 400
    assert client.post('/api/workspace/session', json={'user': 'friend', 'selected': True}, headers=ui).status_code == 200
    assert client.post('/api/workspace/note', json={'user': 'friend', 'note': '  项目负责人 <script>  '}, headers=ui).status_code == 200
    results = client.get('/api/external/sessions', params={'q': '项目负责人'}, headers=external).json()['items']
    assert len(results) == 1 and results[0]['note'] == '项目负责人 <script>'
    assert client.get('/api/external/context', params={'user': 'friend'}, headers=external).json()['note'] == results[0]['note']
    assert client.post('/api/workspace/note', json={'user': 'friend', 'note': 'x'*2001}, headers=ui).status_code == 422
    # Same account reloads selections; another account with the same user ID does not.
    original_root = w.root
    w.root = original_root.parent.parent/'other_account'/'db_storage'
    assert w.workspace_sessions() == []
    assert client.get('/api/external/messages', params={'user': 'friend'}, headers=external).status_code == 403
    w.root = original_root
    assert w.workspace_sessions()[0]['note'] == results[0]['note']
    reloaded = Wechat(); reloaded.root = original_root
    assert reloaded.in_workspace('friend')
    assert client.post('/api/workspace/session', json={'user': 'friend', 'selected': False}, headers=ui).status_code == 200
    assert client.get('/api/external/sessions', headers=external).json()['total'] == 0
    # Reject before touching database or saved context after removal.
    assert any(s['id']=='friend' for s in client.get('/api/session-catalog',headers=ui).json()['items'])
    monkeypatch.setattr(w, 'messages', lambda *args, **kwargs: pytest.fail('Read removed conversation'))
    assert client.get('/api/external/messages', params={'user': 'friend', 'format': 'markdown'}, headers=external).status_code == 403
    assert client.get('/api/external/context', params={'user': 'friend'}, headers=external).status_code == 403


def test_real_stdio_mcp_client(desktop):
    _, server, access, _, tmp_path = desktop
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    http_server = uvicorn.Server(uvicorn.Config(server.app, log_level='error', log_config=None))
    thread = threading.Thread(target=http_server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if http_server.started: break
            time.sleep(.02)
        assert http_server.started
        access.publish(f'http://127.0.0.1:{port}')
        access.configure(True)

        async def check():
            executable = os.environ.get('WECHAT_HELPER_TEST_MCP_EXE')
            parameters = StdioServerParameters(command=executable or sys.executable,
                args=[] if executable else [str(Path(__file__).resolve().parents[1]/'mcp_main.py')],
                env={**os.environ, 'WECHAT_HELPER_DATA': str(tmp_path)})
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    assert {t.name for t in tools} == {'wechat_status', 'list_sessions', 'read_messages', 'search_messages', 'get_chat_context', 'export_chat'}
                    assert all(t.annotations.readOnlyHint for t in tools)
                    for name, arguments in (
                        ('wechat_status', {}), ('list_sessions', {'query': '朋友'}),
                        ('read_messages', {'user': 'friend', 'limit': 2}),
                        ('search_messages', {'user': 'friend', 'query': '消息1'}),
                        ('get_chat_context', {'user': 'friend'}),
                        ('export_chat', {'user': 'friend', 'format': 'markdown', 'limit': 2})):
                        result = await session.call_tool(name, arguments)
                        assert not result.isError, result
                        data = json.loads(result.content[0].text)
                        if name == 'read_messages': assert len(data['items']) == 2 and data['total'] == 6
                        if name == 'search_messages': assert data['total'] == 2
                        if name == 'export_chat': assert data['markdown'].count('### ') == 2
                    assert (await session.list_prompts()).prompts[0].name == 'summarize_chat'
                    prompt = await session.get_prompt('summarize_chat', {'user': 'friend'})
                    assert 'friend' in prompt.messages[0].content.text
                    invalid = await session.call_tool('read_messages', {'user': 'friend', 'limit': 501})
                    assert invalid.isError
                    server.wechat.update_workspace('friend', False)
                    assert (await session.call_tool('list_sessions', {})).isError is False
                    for tool in ('read_messages', 'search_messages', 'get_chat_context', 'export_chat'):
                        arguments={'user': 'friend'}
                        if tool=='search_messages':arguments['query']='消息'
                        assert (await session.call_tool(tool, arguments)).isError
                    access.configure(False)
                    revoked = await session.call_tool('wechat_status', {})
                    assert revoked.isError

        asyncio.run(check())
    finally:
        http_server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_removing_session_cancels_active_analysis(desktop, monkeypatch):
    client, server, _, w, _ = desktop
    entered=threading.Event()
    async def pending_advice(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(server.ai, 'config', lambda: {'context_messages': 10})
    monkeypatch.setattr(server.ai, 'advise', pending_advice)
    ui={'X-Helper-Token':server.TOKEN}
    with client:
        key=client.post('/api/ai/run',json={'user':'friend'},headers=ui).json()['id']
        assert entered.wait(2)
        assert client.post('/api/workspace/session',json={'user':'friend','selected':False},headers=ui).status_code==200
        for _ in range(100):
            if server.jobs[key]['state']=='cancelled':break
            time.sleep(.01)
        assert server.jobs[key]['state']=='cancelled'
        assert client.get('/api/jobs/'+key,headers=ui).status_code==403
