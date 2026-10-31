import sqlite3
import shutil
import pytest
import httpx
from fastapi.testclient import TestClient
from backend.platform_app import create_app
from test_integration import desktop
from test_server import live_server
from test_platform import platform, user, UI
from helper.platform_sync import platform_uploader, PlatformUploader


def test_windows_parser_encrypted_upload_resume_contact_and_all_scope(desktop, platform, monkeypatch):
    _, _, _, w, tmp = desktop
    (w.root/'contact').mkdir(parents=True)
    (w.root/'message').mkdir()
    with sqlite3.connect(w.root/'contact'/'contact.db') as con:
        con.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,local_type INTEGER DEFAULT 1,flag INTEGER DEFAULT 3,verify_flag INTEGER DEFAULT 0,delete_flag INTEGER DEFAULT 0)')
        con.executemany('INSERT INTO contact(username,nick_name,remark) VALUES (?,?,?)', [('friend','朋友 <甲>',''),('wxid_me','本人','')])
    for name, (_, path) in list(w.cache.items()):
        shutil.copy2(path,w.root/name)
    web, _ = user(platform)
    _, service, app = platform
    created = web.post('/api/v2/clients', headers=UI, json={'name':'Windows test'}).json()
    collector = PlatformUploader()
    monkeypatch.setattr('helper.platform_sync.platform_uploader', collector)
    with live_server(create_app(service, start_worker=False)) as url:
        collector.configure(w, url, created['app_key'], created['app_secret'], True, 'selected')
        assert collector.test(w)['ok']
        result = collector.sync(w)
        assert result['state'] == 'done' and result['uploaded'] == 6, result
        assert web.get('/api/v2/contacts').json()['total'] == 1
        session = web.get('/api/v2/sessions').json()['items'][0]
        assert web.get(f"/api/v2/sessions/{session['id']}/messages").json()['total'] == 6
        assert collector.sync(w)['state'] == 'done'
        assert web.get(f"/api/v2/sessions/{session['id']}/messages").json()['total'] == 6
        assert created['app_secret'].encode() not in (tmp/'platform-sync.bin').read_bytes()
        assert created['app_secret'] not in str(collector.settings(w))
        # All-account collection is independent of the old local workspace selection.
        collector.configure(w, url, created['app_key'], '', True, 'all')
        w.update_workspace('friend', False)
        assert collector.sync(w)['state'] == 'done'
        assert web.get('/api/v2/contacts').json()['total'] >= 2
        # Disable upload locally; no further parsed data is sent.
        collector.configure(w, url, created['app_key'], '', False, 'all')
        assert collector.sync(w)['state'] == 'disabled'


def test_platform_client_secret_never_returned_by_local_api(desktop):
    client, server, _, w, _ = desktop
    ui = {'X-Helper-Token':server.TOKEN}
    assert client.get('/api/platform/settings').status_code == 401
    result = client.post('/api/platform/settings', headers=ui, json={
        'url':'http://127.0.0.1:8787','app_key':'app_'+'a'*32,'app_secret':'secret_'+'b'*32,'enabled':True,'scope':'all'})
    assert result.status_code == 200
    assert 'b'*32 not in result.text and result.json()['has_secret']
    result = client.post('/api/platform/settings', headers=ui, json={
        'url':'https://different.example','app_key':'app_'+'a'*32,'enabled':True,'scope':'all'})
    assert result.status_code == 400


@pytest.mark.parametrize('status', [404, 503])
def test_priority_list_failure_falls_back_to_ordinary_sync(status):
    class Client:
        def get(self, *args, **kwargs):
            return httpx.Response(status, request=httpx.Request('GET', 'https://example.test'))
    assert PlatformUploader().priority_sessions(Client(), 'a'*64) is None
