import hashlib
import shutil
import sqlite3
from contextlib import closing
from sqlalchemy import delete

from backend.platform_app import create_app
from helper.platform_sync import PlatformUploader
from test_integration import desktop
from test_platform import platform, user, UI
from test_server import live_server


def prepare(desktop):
    _,_,_,w,_=desktop
    for folder in ('contact','message'):
        (w.root/folder).mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(w.root/'contact/contact.db')) as con, con:
        con.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,local_type INTEGER,flag INTEGER,verify_flag INTEGER,delete_flag INTEGER)')
        con.execute("INSERT INTO contact VALUES ('friend','朋友','',1,3,0,0)")
    for name,(_,path) in list(w.cache.items()): shutil.copy2(path,w.root/name)
    return w


def test_recent_first_backfill_resumes_and_idle_round_sends_nothing(desktop,platform,monkeypatch):
    from helper import platform_sync
    w=prepare(desktop)
    table='Msg_'+hashlib.md5(b'friend').hexdigest()
    with closing(sqlite3.connect(w.root/'message/message_0.db')) as con,con:
        con.executemany(f'INSERT INTO [{table}] VALUES (?,?,?,?,?)',
            [(i,2,1,1728000000+i,'历史消息'+str(i)) for i in range(3,1003)])
    uploader=PlatformUploader(); monkeypatch.setattr(platform_sync,'platform_uploader',uploader)
    client,_=user(platform); _,service,_=platform
    credentials=client.post('/api/v2/clients',headers=UI,json={'name':'incremental test'}).json()
    payloads=[]; original=platform_sync.envelope
    def capture(config,payload): payloads.append(payload); return original(config,payload)
    monkeypatch.setattr(platform_sync,'envelope',capture)
    with live_server(create_app(service,start_worker=False)) as url:
        uploader.configure(w,url,credentials['app_key'],credentials['app_secret'],True,'all')
        first=uploader.sync(w)
        assert first['state']=='done' and first['history_pending']
        sid=client.get('/api/v2/sessions').json()['items'][0]['id']
        page=client.get(f'/api/v2/sessions/{sid}/messages?limit=1').json()
        assert page['items'][0]['text']=='历史消息1002'
        assert page['total']<1006
        # Simulate restarting the client halfway through history, retaining DPAPI checkpoints.
        uploader=PlatformUploader(); monkeypatch.setattr(platform_sync,'platform_uploader',uploader)
        for _ in range(20):
            result=uploader.sync(w)
            assert result['state']=='done',result
            if not result['history_pending']:break
        else: raise AssertionError('history did not converge')
        assert client.get(f'/api/v2/sessions/{sid}/messages?limit=1').json()['total']==1006
        payloads.clear()
        from helper import storage
        progress = storage.read_json('sync-progress.bin', {}, secret=True)
        previous_audit = next(iter(progress.values()))['audit_at']
        assert uploader.sync(w)['uploaded']==0
        assert payloads==[]  # No unchanged manifest, previews or message envelopes.
        assert next(iter(storage.read_json('sync-progress.bin', {}, secret=True).values()))['audit_at'] == previous_audit
        progress = storage.read_json('sync-progress.bin', {}, secret=True)
        next(iter(progress.values()))['audit_at'] = previous_audit - 301
        storage.write_json('sync-progress.bin', progress, secret=True)
        assert uploader.sync(w)['uploaded'] == 0
        assert any(payload['kind'] == 'manifest' for payload in payloads)
        assert all(payload['kind'] != 'messages' for payload in payloads)
        # A new message uploads once; changing a recent record is also propagated.
        with closing(sqlite3.connect(w.root/'message/message_0.db')) as con,con:
            con.execute(f'INSERT INTO [{table}] VALUES (?,?,?,?,?)',(1003,2,1,1728002000,'新消息'))
            con.execute(f'UPDATE [{table}] SET message_content=? WHERE local_id=1002',('修改后的消息',))
        result=uploader.sync(w)
        assert result['state']=='done' and result['uploaded']==2,result
        assert client.get(f'/api/v2/sessions/{sid}/messages?limit=1').json()['total']==1007
        # Periodic reconciliation recovers from a server restore without trusting local acknowledgements.
        from backend.platform_db import messages, sessions, contacts, accounts
        from helper import storage
        with service.db.transaction() as con:
            for table_ in (messages, sessions, contacts, accounts):
                con.execute(delete(table_))
        progress=storage.read_json('sync-progress.bin',{},secret=True)
        for state in progress.values():state['audit_at']=0
        storage.write_json('sync-progress.bin',progress,secret=True)
        for _ in range(20):
            result=uploader.sync(w)
            assert result['state']=='done',result
            if not result['history_pending']:break
        restored=client.get('/api/v2/sessions').json()['items']
        assert len(restored)==1 and restored[0]['message_count']==1007


def test_failed_upload_does_not_advance_local_acknowledgement(desktop,platform,monkeypatch):
    from helper import platform_sync
    w=prepare(desktop); uploader=PlatformUploader()
    monkeypatch.setattr(platform_sync,'platform_uploader',uploader)
    client,_=user(platform);_,service,_=platform
    credentials=client.post('/api/v2/clients',headers=UI,json={'name':'retry test'}).json()
    original=platform_sync.envelope
    def fail(config,payload):
        if payload['kind']=='messages':raise ValueError('合成网络中断')
        return original(config,payload)
    with live_server(create_app(service,start_worker=False)) as url:
        uploader.configure(w,url,credentials['app_key'],credentials['app_secret'],True,'all')
        monkeypatch.setattr(platform_sync,'envelope',fail)
        assert uploader.sync(w)['state']=='error'
        monkeypatch.setattr(platform_sync,'envelope',original)
        result=uploader.sync(w)
        assert result['state']=='done' and uploader.changed_messages==6,result


def test_listening_sessions_run_first_without_starving_normal_history(desktop, platform, monkeypatch):
    from helper import platform_sync
    w = prepare(desktop)
    with closing(sqlite3.connect(w.root/'contact/contact.db')) as con, con:
        con.execute("INSERT INTO contact VALUES ('friend2','重点朋友','',1,3,0,0)")
    first_table = 'Msg_' + hashlib.md5(b'friend').hexdigest()
    high_table = 'Msg_' + hashlib.md5(b'friend2').hexdigest()
    with closing(sqlite3.connect(w.root/'message/message_0.db')) as con, con:
        con.execute("INSERT INTO Name2Id(user_name) VALUES ('friend2')")
        con.execute(f'CREATE TABLE [{high_table}](local_id INTEGER,real_sender_id INTEGER,local_type INTEGER,create_time INTEGER,message_content TEXT)')
        for table, start in ((first_table, 3), (high_table, 0)):
            con.executemany(f'INSERT INTO [{table}] VALUES (?,?,?,?,?)',
                [(i, 2, 1, 1728000000+i, '历史'+str(i)) for i in range(start, 2000)])
    uploader = PlatformUploader()
    monkeypatch.setattr(platform_sync, 'platform_uploader', uploader)
    client, _ = user(platform); _, service, _ = platform
    credentials = client.post('/api/v2/clients', headers=UI, json={'name': 'priority test'}).json()
    payloads = []; original = platform_sync.envelope
    def capture(config, payload):
        payloads.append(payload)
        return original(config, payload)
    monkeypatch.setattr(platform_sync, 'envelope', capture)
    with live_server(create_app(service, start_worker=False)) as url:
        uploader.configure(w, url, credentials['app_key'], credentials['app_secret'], True, 'all')
        assert uploader.sync(w)['history_pending']
        entries = client.get('/api/v2/sessions').json()['items']
        high = next(s for s in entries if s['name'] == '重点朋友')
        assert client.post(f"/api/v2/sessions/{high['id']}/listener", headers=UI, json={'enabled': True}).status_code == 200
        payloads.clear()
        result = uploader.sync(w)
        assert result['state'] == 'done' and result['priority_count'] == 1 and result['priority_available']
        uploaded = [p['session'] for p in payloads if p['kind'] == 'messages']
        assert uploaded[0] == 'friend2' and 'friend' in uploaded
        assert client.post(f"/api/v2/sessions/{high['id']}/listener", headers=UI, json={'enabled': False}).status_code == 200
        assert uploader.sync(w)['priority_count'] == 0


def test_priority_messages_precede_large_ordinary_manifest(desktop, platform, monkeypatch):
    from helper import platform_sync
    w = prepare(desktop)
    uploader = PlatformUploader(); monkeypatch.setattr(platform_sync, 'platform_uploader', uploader)
    client, _ = user(platform); _, service, _ = platform
    credentials = client.post('/api/v2/clients', headers=UI, json={'name':'priority before manifest'}).json()
    payloads = []; original = platform_sync.envelope
    def capture(config, payload):
        payloads.append(payload)
        return original(config, payload)
    monkeypatch.setattr(platform_sync, 'envelope', capture)
    table = 'Msg_' + hashlib.md5(b'friend').hexdigest()
    with live_server(create_app(service, start_worker=False)) as url:
        uploader.configure(w, url, credentials['app_key'], credentials['app_secret'], True, 'all')
        assert uploader.sync(w)['state'] == 'done'
        sid = client.get('/api/v2/sessions').json()['items'][0]['id']
        client.post(f'/api/v2/sessions/{sid}/listener', headers=UI, json={'enabled':True})
        with closing(sqlite3.connect(w.root/'contact/contact.db')) as con, con:
            con.executemany('INSERT INTO contact VALUES (?,?,?,?,?,?,?)',
                [(f'ordinary_{i}', f'普通联系人{i}', '', 1, 3, 0, 0) for i in range(600)])
        with closing(sqlite3.connect(w.root/'message/message_0.db')) as con, con:
            con.execute(f'INSERT INTO [{table}] VALUES (?,?,?,?,?)', (4,2,1,1728000099,'重点新增'))
        payloads.clear()
        result = uploader.sync(w)
        assert result['state'] == 'done' and result['priority_count'] == 1
        target_index = next(i for i,p in enumerate(payloads) if p['kind']=='messages' and p['session']=='friend')
        bulk_index = next(i for i,p in enumerate(payloads) if p['kind']=='manifest' and
                          any(c['id'].startswith('ordinary_') for c in p['contacts']))
        assert target_index < bulk_index
        assert client.get(f'/api/v2/sessions/{sid}/messages?q=重点新增').json()['total'] == 1


def test_interrupted_manifest_audit_resumes_without_reuploading_acknowledged_pages(desktop, monkeypatch):
    from types import SimpleNamespace
    from helper import incremental_sync
    real_time = incremental_sync.time
    clock = [0]
    monkeypatch.setattr(incremental_sync, 'time', SimpleNamespace(time=real_time.time, time_ns=real_time.time_ns,
        monotonic=lambda: clock[0]))
    _, _, _, w, _ = desktop
    config = {'url':'http://127.0.0.1:1','app_key':'app_test'}
    common = {'source':'a'*64}
    catalog = [{'id':f'session_{i}','name':f'会话{i}'} for i in range(450)]
    sent = []
    def new_sync():
        sync = incremental_sync.IncrementalSync(None, w, config, common, None, lambda:None)
        def post(payload):
            sent.extend(item['id'] for item in payload['sessions'])
            clock[0] += 1
            return {}
        monkeypatch.setattr(sync, 'post', post)
        return sync
    first = new_sync()
    assert not first.manifest(catalog, [], deadline=1)
    first.finish(audit_complete=False)
    second = new_sync()
    assert second.audit and second.audit_epoch == first.audit_epoch
    assert second.manifest(catalog, [], deadline=10)
    assert len(sent) == len(catalog) and len(set(sent)) == len(catalog)


def test_no_cursor_requests_for_sessions_without_message_tables(desktop):
    from helper.incremental_sync import IncrementalSync
    _, _, _, w, _ = desktop
    class Uploader:
        states = {'a'*64: {}}
        def allowed(self, w, user): return True
    class Client:
        def get(self, *args, **kwargs): raise AssertionError('no message table must not trigger cursor requests')
    sync = IncrementalSync(Uploader(), w, {'url':'http://localhost','app_key':'app_test'}, {'source':'a'*64}, Client(), lambda:None)
    assert not sync.messages([{'id':'no_table','name':'无消息'}], list(w.tables))
