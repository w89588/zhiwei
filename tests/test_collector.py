import hashlib
import shutil
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend.platform_app import create_app
from helper.collector import Collector
from helper.platform_sync import PlatformUploader
from test_integration import desktop
from test_platform import platform, user, UI
from test_server import live_server


def wait_for(predicate, timeout=15):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('timed out waiting for collector state')


def test_configuration_precedes_parser_and_rejects_invalid_server(desktop, monkeypatch):
    from helper import collector as module
    _, _, _, w, tmp = desktop
    collector = Collector()
    monkeypatch.setattr(module, 'wechat', w)
    monkeypatch.setattr(module, 'discover', lambda: [])
    monkeypatch.setattr(collector, 'verify', lambda config: (_ for _ in ()).throw(ValueError('连接失败')))
    with pytest.raises(ValueError, match='连接失败'):
        collector.configure('http://127.0.0.1:8787', 'app_'+'a'*32, 'secret_'+'b'*32, True)
    assert not (tmp/'collector.bin').exists()
    with pytest.raises(ValueError, match='授权'):
        collector.configure('http://127.0.0.1:8787', 'app_'+'a'*32, 'secret_'+'b'*32, False)


def test_local_settings_accepts_lan_http(desktop, monkeypatch):
    from helper import collector as module, collector_app
    _, _, _, w, _ = desktop
    collector = Collector()
    monkeypatch.setattr(module, 'wechat', w)
    monkeypatch.setattr(module, 'discover', lambda: [])
    verified = []
    monkeypatch.setattr(collector, 'verify', lambda config: verified.append(config['url']))
    monkeypatch.setattr(collector_app, 'collector', collector)
    client = TestClient(collector_app.app)
    response = client.post('/api/settings', headers={'X-Helper-Token': collector_app.TOKEN}, json={
        'url': 'http://192.168.1.20:8787', 'app_key': 'app_'+'a'*32,
        'app_secret': 'secret_'+'b'*32, 'consent': True})
    assert response.status_code == 200, response.text
    assert verified == ['http://192.168.1.20:8787']
    assert collector.config()['url'] == 'http://192.168.1.20:8787'


def test_collector_uploads_and_counts_only_server_changes(desktop, platform, monkeypatch):
    from helper import collector as module
    _, _, _, w, tmp = desktop
    (w.root/'contact').mkdir(parents=True)
    (w.root/'message').mkdir()
    with sqlite3.connect(w.root/'contact'/'contact.db') as con:
        con.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,local_type INTEGER DEFAULT 1,flag INTEGER DEFAULT 3,verify_flag INTEGER DEFAULT 0,delete_flag INTEGER DEFAULT 0)')
        con.executemany('INSERT INTO contact(username,nick_name,remark) VALUES (?,?,?)', [('friend','朋友',''),('wxid_me','本人','')])
    for name, (_, path) in list(w.cache.items()):
        shutil.copy2(path, w.root/name)
    w.cache.clear()
    w.tables.clear()
    w.status['state'] = 'idle'
    uploader = PlatformUploader()
    collector = Collector()
    monkeypatch.setattr(module, 'wechat', w)
    monkeypatch.setattr(module, 'platform_uploader', uploader)
    monkeypatch.setattr('helper.platform_sync.platform_uploader', uploader)
    monkeypatch.setattr(module, 'discover', lambda: [{'path': str(w.root), 'account': '测试账号'}])
    web, _ = user(platform)
    _, service, _ = platform
    created = web.post('/api/v2/clients', headers=UI, json={'name':'collector test'}).json()
    with live_server(create_app(service, start_worker=False)) as url:
        settings = collector.configure(url, created['app_key'], created['app_secret'], True)
        assert settings['configured'] and settings['server_state'] == 'ready'
        assert created['app_secret'] not in str(settings)
        assert created['app_secret'].encode() not in (tmp/'collector.bin').read_bytes()
        stop = threading.Event()
        worker = threading.Thread(target=collector.loop, args=(stop,), daemon=True)
        worker.start()
        try:
            wait_for(lambda: uploader.changed_messages == 6)
            collector.changed.set()
            wait_for(lambda: uploader.states and next(iter(uploader.states.values()))['state'] == 'done')
            time.sleep(.5)
            assert uploader.changed_messages == 6
            table = 'Msg_' + hashlib.md5(b'friend').hexdigest()
            with sqlite3.connect(w.root/'message'/'message_0.db') as con:
                con.execute(f'INSERT INTO [{table}] VALUES (?,?,?,?,?)', (9, 2, 1, 1728000010, '新消息'))
            collector.changed.set()
            try:
                wait_for(lambda: uploader.changed_messages >= 7)
            except AssertionError:
                pytest.fail(f'collector={collector.status()}, wechat={w.status}, states={uploader.states}')
            assert uploader.changed_messages == 7
            assert web.get('/api/v2/sessions').json()['items'][0]['message_count'] == 7
        finally:
            stop.set(); collector.changed.set(); worker.join(10)
            assert not worker.is_alive()


def test_local_surface_rejects_unauthenticated_requests(desktop, monkeypatch):
    from helper import collector_app
    _, _, _, _, _ = desktop
    monkeypatch.setattr(collector_app, 'collector', Collector())
    client = TestClient(collector_app.app)
    assert client.get('/api/status').status_code == 401
    assert client.get('/api/status', headers={'X-Helper-Token':collector_app.TOKEN}).status_code == 200
    assert client.get('/api/status', headers={'X-Helper-Token':collector_app.TOKEN, 'Origin':'https://evil.example'}).status_code == 403
    assert client.get('/api/messages', headers={'X-Helper-Token':collector_app.TOKEN}).status_code == 404


def test_partial_database_failure_does_not_report_success(desktop, monkeypatch):
    _, _, _, w, _ = desktop
    uploader = PlatformUploader()
    uploader.configure(w, 'http://127.0.0.1:8787', 'app_'+'a'*32, 'secret_'+'b'*32, True, 'all')
    monkeypatch.setattr(w, 'sync', lambda **kwargs: w.status.update(errors=[{'database':'message/message_0.db', 'error':'read failed'}]))
    result = uploader.sync(w)
    assert result['state'] == 'error' and '读取失败' in result['message']


@pytest.mark.parametrize('priority_count,expected', [(1, 10), (0, 13)])
def test_collector_waits_from_start_only_for_priority_rounds(monkeypatch, priority_count, expected):
    from types import SimpleNamespace
    from helper import collector as module
    clock = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    collector = Collector()
    config = {'version': 'v1', 'url': 'http://localhost', 'app_key': 'app_key',
              'app_secret': 'secret', 'account_path': 'account'}
    monkeypatch.setattr(collector, 'config', lambda: config)
    collector.verified_version = 'v1'
    monkeypatch.setattr(module, 'wechat', SimpleNamespace(root='account', status={'state': 'ready', 'message': 'ready'}))
    monkeypatch.setattr(module, 'discover', lambda: [{'path': 'account'}])
    def sync(_):
        clock[0] += 3
        return {'state': 'done', 'priority_count': priority_count}
    monkeypatch.setattr(module, 'platform_uploader', SimpleNamespace(configure=lambda *a: None, sync=sync))
    class Stop:
        def is_set(self): return clock[0] >= expected
        def wait(self, seconds):
            clock[0] += seconds
            return self.is_set()
    collector.loop(Stop())
    assert clock[0] == expected


def test_tray_close_hides_only_after_icon_is_ready():
    from types import SimpleNamespace
    from main import Tray
    calls = []
    window = SimpleNamespace(native=SimpleNamespace(Hide=lambda: calls.append('hide')), destroy=lambda: calls.append('destroy'))
    tray = Tray(window)
    assert tray.closing() is None
    tray.icon = SimpleNamespace(Visible=True, Dispose=lambda: calls.append('dispose'))
    assert tray.closing() is False and calls == ['hide']
    tray.exit()
    assert tray.exiting and calls == ['hide', 'dispose', 'destroy']
    assert tray.closing() is None
