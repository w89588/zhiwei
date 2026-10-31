import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from helper.remote_sync import Uploader, validate_url
from test_integration import desktop
from test_server import ACCOUNTS, READ, live_server


def test_upload_real_parser_resume_edit_and_removal(desktop):
    _, _, _, w, tmp_path = desktop
    uploader = Uploader()
    backend = create_app(tmp_path/'server.sqlite3', ACCOUNTS)
    reader = TestClient(backend)
    with live_server(backend) as url:
        uploader.configure(w, url, 'u'*32, True)
        assert uploader.test(w)['ok']
        result = uploader.sync(w)
        assert result['state'] == 'done' and result['uploaded'] == 6, result
        response = reader.get('/api/external/messages?user=friend', headers=READ).json()
        assert response['total'] == 6 and len({m['id'] for m in response['items']}) == 6
        assert all(m['sender'] in {'朋友 <甲>', '我'} for m in response['items'])
        # Retries/upserts preserve distinct shards, including identical local IDs.
        assert uploader.sync(w, force=True)['state'] == 'done'
        assert reader.get('/api/external/messages?user=friend', headers=READ).json()['total'] == 6
        path = w.cache['message/message_0.db'][1]
        table = next(t for t in w.tables['message/message_0.db'] if t.startswith('Msg_'))
        with sqlite3.connect(path) as con:
            con.execute(f'INSERT INTO [{table}] VALUES (?,?,?,?,?)', (3, 2, 1, 1728000010, '新增消息'))
            con.execute(f'UPDATE [{table}] SET message_content=? WHERE local_id=2', ('撤回后内容',))
        w.status['generation'] += 1
        assert uploader.sync(w)['state'] == 'done'
        messages = reader.get('/api/external/messages?user=friend', headers=READ).json()
        assert messages['total'] == 7 and any(m['text'] == '撤回后内容' for m in messages['items'])
        # A simulated network failure after the first committed batch is safe to retry.
        request = uploader.request
        def flaky(client, method, path, **kwargs):
            if path == 'api/v1/client/messages':
                request(client, method, path, **kwargs)
                raise ValueError('模拟断线')
            return request(client, method, path, **kwargs)
        uploader.request = flaky
        assert uploader.sync(w, full=True)['state'] == 'error'
        uploader.request = request
        assert uploader.sync(w, full=True)['state'] == 'done'
        assert reader.get('/api/external/messages?user=friend', headers=READ).json()['total'] == 7
        # New source snapshots during an upload must not starve a long history transfer.
        def advancing(client, method, path, **kwargs):
            result = request(client, method, path, **kwargs)
            if path == 'api/v1/client/messages':
                w.status['generation'] += 1
            return result
        uploader.request = advancing
        assert uploader.sync(w, full=True)['state'] == 'done'
        uploader.request = request
        # A restored/reset server must be detected even if no local messages changed.
        with backend.state.store.connect(True) as con:
            con.execute('DELETE FROM messages')
            con.execute('DELETE FROM sessions')
            con.execute('DELETE FROM accounts')
        assert uploader.sync(w)['state'] == 'done'
        assert reader.get('/api/external/messages?user=friend', headers=READ).json()['total'] == 7
        w.update_workspace('friend', False)
        assert uploader.sync(w)['state'] == 'done'
        assert reader.get('/api/external/messages?user=friend', headers=READ).status_code == 403
        assert reader.get('/api/external/sessions', headers=READ).json()['total'] == 0


def test_upload_config_account_scope_and_token_protection(desktop):
    client, server, _, w, tmp_path = desktop
    ui = {'X-Helper-Token': server.TOKEN}
    assert client.get('/api/remote/settings').status_code == 401
    response = client.post('/api/remote/settings', headers=ui, json={'url': 'https://chat.example.com', 'token': 'u'*32, 'enabled': True})
    assert response.status_code == 200, response.text
    assert 'u'*32 not in response.text and response.json()['has_token']
    assert b'u'*32 not in (tmp_path/'remote-sync.bin').read_bytes()
    assert client.post('/api/remote/settings', headers=ui, json={'url': 'https://elsewhere.example.com', 'enabled': True}).status_code == 400
    old = w.root
    w.root = tmp_path/'another_account'/'db_storage'
    assert not client.get('/api/remote/settings', headers=ui).json()['enabled']
    w.root = old
    assert client.post('/api/remote/settings', headers=ui, json={'url': 'https://chat.example.com', 'enabled': False}).status_code == 200
    assert client.post('/api/remote/sync', headers=ui, json={}).status_code == 400


@pytest.mark.parametrize('url', ['https://user:pass@example.com', 'https://example.com/?secret=1',
                                 'file:///data', 'https://example.com/#a', 'http://127.0.0.1:bad', 'https://example.com\\@evil.com'])
def test_remote_url_validation(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize('url', ['http://192.168.1.10:8787', 'http://192.168.1.20:8787',
                                 'http://minipc.local:8787', 'http://[fd00::1]:8787',
                                 'https://example.com'])
def test_server_url_accepts_http_and_https(url):
    assert validate_url(url + '/') == url


def test_remote_stdio_bridge_uses_only_read_token(desktop, monkeypatch):
    from helper import mcp_server
    _, _, _, _, tmp_path = desktop
    app = create_app(tmp_path/'server.sqlite3', ACCOUNTS)
    with live_server(app) as url:
        monkeypatch.setenv('WECHAT_HELPER_SERVER_URL', url)
        monkeypatch.setenv('WECHAT_HELPER_READ_TOKEN', 'r'*32)
        assert mcp_server.wechat_status()['state'] == 'idle'
        monkeypatch.setenv('WECHAT_HELPER_READ_TOKEN', 'u'*32)
        with pytest.raises(ValueError, match='权限不足'):
            mcp_server.wechat_status()
