import asyncio
from contextlib import contextmanager
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from backend.app import create_app


ACCOUNTS = {'first': {'upload_token': 'u'*32, 'read_token': 'r'*32},
            'second': {'upload_token': 'v'*32, 'read_token': 's'*32}}
UPLOAD = {'Authorization': 'Bearer '+'u'*32}
READ = {'Authorization': 'Bearer '+'r'*32}
OTHER_READ = {'Authorization': 'Bearer '+'s'*32}
MANIFEST = {'source': 'a'*64, 'sessions': [{'id': 'friend', 'name': '朋友', 'note': '项目负责人',
             'context': {'profile': {'goals': '沟通目标'}}}]}


def message(i, **changes):
    return {'id': f'message/0.db:{i}', 'database': 'message/0.db', 'local_id': i,
            'timestamp': 100+i, 'text': f'消息 {i}', 'sender': '朋友', **changes}


@pytest.fixture
def gateway(tmp_path):
    return TestClient(create_app(tmp_path/'chats.sqlite3', ACCOUNTS))


def upload(client, items=None):
    revision = client.post('/api/v1/client/workspace', json=MANIFEST, headers=UPLOAD).json()['revision']
    result = client.post('/api/v1/client/messages', headers=UPLOAD,
                         json={'revision': revision, 'user': 'friend', 'items': items or [message(i) for i in range(6)]})
    assert result.status_code == 200, result.text
    return revision


def test_persistence_paging_search_and_account_isolation(gateway, tmp_path):
    upload(gateway)
    # A new app instance and no running client: only persisted data is used.
    client = TestClient(create_app(tmp_path/'chats.sqlite3', ACCOUNTS))
    status = client.get('/api/external/status', headers=READ).json()
    assert status['state'] == 'ready' and status['workspace_count'] == 1
    assert client.get('/api/external/sessions', headers=OTHER_READ).json()['total'] == 0
    assert client.get('/api/external/messages?user=friend', headers=OTHER_READ).status_code == 403
    pages = [client.get('/api/external/messages', params={'user': 'friend', 'offset': i, 'limit': 2}, headers=READ).json() for i in (0, 2, 4)]
    assert [[m['local_id'] for m in p['items']] for p in pages] == [[4, 5], [2, 3], [0, 1]]
    assert pages[0]['next_offset'] == 2 and pages[-1]['next_offset'] is None
    result = client.get('/api/external/messages?user=friend&q=消息%202&start=102&end=102', headers=READ).json()
    assert result['total'] == 1 and result['items'][0]['local_id'] == 2
    context = client.get('/api/external/context?user=friend', headers=READ).json()
    assert context['note'] == '项目负责人' and context['profile']['goals'] == '沟通目标'
    markdown = client.get('/api/external/messages?user=friend&format=markdown', headers=READ).json()
    assert '消息数：6' in markdown['markdown'] and 'items' not in markdown


def test_search_link_metadata_in_existing_stored_messages(gateway):
    upload(gateway, [message(1, type=49, text='分享标题', extra={'url':'https://example.com/path', 'des':'描述内容'})])
    result = gateway.get('/api/external/messages', params={'user':'friend', 'q':'example.com/path'}, headers=READ)
    assert result.status_code == 200 and result.json()['total'] == 1


def test_scope_auth_revocation_and_stale_uploads(gateway):
    client = gateway
    assert client.get('/healthz').status_code == 200
    for headers in ({}, UPLOAD, {'Authorization': b'Bearer \xff'}):
        assert client.get('/api/external/status', headers=headers).status_code == 401
    assert client.get('/api/v1/client/status', headers=READ).status_code == 401
    assert client.post('/api/v1/client/workspace', headers=READ, json=MANIFEST).status_code == 401
    assert client.post('/mcp', headers=UPLOAD, json={}).status_code == 401
    assert client.get('/api/external/status', headers={**READ, 'Origin': 'https://evil.example'}).status_code == 403
    revision = upload(client)
    # Removal hides stored messages/context and invalidates any in-flight upload generation.
    assert client.post('/api/v1/client/workspace', headers=UPLOAD, json={**MANIFEST, 'sessions': []}).status_code == 200
    assert client.get('/api/external/sessions', headers=READ).json()['total'] == 0
    for endpoint in ('messages', 'context'):
        assert client.get('/api/external/'+endpoint+'?user=friend', headers=READ).status_code == 403
    batch = {'revision': revision, 'user': 'friend', 'items': [message(7)]}
    assert client.post('/api/v1/client/messages', headers=UPLOAD, json=batch).status_code == 409
    # Re-adding restores history, and one credential cannot be rebound to another source.
    client.post('/api/v1/client/workspace', headers=UPLOAD, json=MANIFEST)
    assert client.get('/api/external/messages?user=friend', headers=READ).json()['total'] == 6
    assert client.post('/api/v1/client/workspace', headers=UPLOAD, json={**MANIFEST, 'source': 'b'*64}).status_code == 409


def test_atomic_idempotent_batches_and_validation(gateway):
    client = gateway
    revision = upload(client)
    updated = message(2, text='更新后的撤回记录')
    for _ in range(2):
        assert client.post('/api/v1/client/messages', headers=UPLOAD, json={'revision': revision, 'user': 'friend', 'items': [updated]}).status_code == 200
    assert client.get('/api/external/messages?user=friend', headers=READ).json()['total'] == 6
    assert client.get('/api/external/messages?user=friend&q=撤回', headers=READ).json()['items'][0]['text'] == updated['text']
    broken = [message(6), message(7, id='invalid')]
    assert client.post('/api/v1/client/messages', headers=UPLOAD, json={'revision': revision, 'user': 'friend', 'items': broken}).status_code == 409
    assert client.get('/api/external/messages?user=friend', headers=READ).json()['total'] == 6
    for query in ('limit=501', 'offset=-1', 'start=5&end=4', 'format=html'):
        assert client.get('/api/external/messages?user=friend&'+query, headers=READ).status_code == 422
    duplicate = {**MANIFEST, 'sessions': MANIFEST['sessions']*2}
    assert client.post('/api/v1/client/workspace', json=duplicate, headers=UPLOAD).status_code == 409
    body = {'revision': revision, 'user': 'friend', 'items': [message(-1, text='private text')]}
    result = client.post('/api/v1/client/messages', json=body, headers=UPLOAD)
    assert result.status_code == 422 and 'private text' not in result.text
    assert client.post('/api/v1/client/messages', content=b'x'*(8*1024*1024+1), headers=UPLOAD).status_code == 413


@contextmanager
def live_server(app):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.02)
        assert server.started
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_actual_http_mcp_tools_and_account_binding(gateway):
    upload(gateway)
    with live_server(gateway.app) as url:
        async def check():
            async def use(headers):
                async with httpx.AsyncClient(headers=headers) as http:
                    async with streamable_http_client(url+'/mcp', http_client=http) as (reader, writer, _):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            tools = (await session.list_tools()).tools
                            assert len(tools) == 6 and all(t.annotations.readOnlyHint for t in tools)
                            result = await session.call_tool('list_sessions', {})
                            data = json.loads(result.content[0].text)
                            assert data['total'] == (1 if headers == READ else 0)
                            if headers == READ:
                                for tool, args in [('wechat_status', {}), ('read_messages', {'user': 'friend'}),
                                    ('search_messages', {'user': 'friend', 'query': '消息'}),
                                    ('get_chat_context', {'user': 'friend'}), ('export_chat', {'user': 'friend'})]:
                                    result = await session.call_tool(tool, args)
                                    assert not result.isError, result
                                assert (await session.list_prompts()).prompts[0].name == 'summarize_chat'
                            else:
                                assert (await session.call_tool('read_messages', {'user': 'friend'})).isError
            await use(READ)
            await use(OTHER_READ)
        asyncio.run(check())


def test_rejects_shared_and_missing_credentials(tmp_path):
    for accounts in ({}, {'a': {'upload_token': 'short', 'read_token': 'r'*32}},
                     {'a': {'upload_token': 'u'*32, 'read_token': 'u'*32}}):
        with pytest.raises(ValueError):
            create_app(tmp_path/'invalid.sqlite3', accounts)
