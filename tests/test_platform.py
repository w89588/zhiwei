"""V2 end-to-end contracts: real crypto, SQL transactions and HTTP/MCP permissions."""
from datetime import datetime
import json
import secrets
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, func

from backend.platform_app import create_app
from backend.platform_db import Database, jobs, mcp_keys, memories, messages, models, row, schema, sessions, users, versions
from backend.platform_documents import TestDocuments as Documents
from backend.platform_jobs import Worker, period, ZONE
from backend.platform_models import ModelConfig, UserCreate
from backend.platform_security import Vault, digest, encode
from backend.platform_service import Platform
from helper.platform_sync import envelope

UI = {'X-Wechat-UI': '1'}
PASSWORD = 'Initial-password-123'
NEW = 'New-test-password-123'


@pytest.fixture
def platform(tmp_path):
    service = Platform(Database('sqlite:///'+str(tmp_path/'platform.sqlite3')), Vault(encode(secrets.token_bytes(32))), Documents())
    service.bootstrap('admin', PASSWORD)
    service.change_password(service.login('admin', PASSWORD)[1]['id'], PASSWORD, NEW)
    app = create_app(service, start_worker=False)
    with TestClient(app) as client:
        assert client.post('/api/v2/auth/login', headers=UI, json={'username':'admin','password':NEW}).status_code == 200
        yield client, service, app
    service.db.engine.dispose()


def user(platform, name='alice'):
    admin, service, app = platform
    result = admin.post('/api/v2/admin/users', headers=UI, json={'username':name, 'name':name, 'password':PASSWORD})
    assert result.status_code == 200, result.text
    client = TestClient(app)
    assert client.post('/api/v2/auth/login', headers=UI, json={'username':name,'password':PASSWORD}).status_code == 200
    assert client.get('/api/v2/overview').status_code == 403
    assert client.post('/api/v2/auth/password', headers=UI, json={'current_password':PASSWORD,'password':NEW}).status_code == 200
    assert client.post('/api/v2/auth/login', headers=UI, json={'username':name,'password':NEW}).status_code == 200
    return client, result.json()['id']


def device(client):
    result = client.post('/api/v2/clients', headers=UI, json={'name':'测试电脑'})
    assert result.status_code == 200, result.text
    config = result.json()
    registration = client.post('/api/v2/client/register', json={**{k:config[k] for k in ('app_key','app_secret')}, 'device_id':'test-device-000001'})
    assert registration.status_code == 200, registration.text
    config.update(registration.json())
    return config


def upload(client, config, payload):
    body = envelope(config, payload)
    return client.post('/api/v2/client/sync', headers={'Authorization':'Bearer '+config['token']}, json=body)


def seed(client, config, source='a'*64, timestamp=1704067200, self_id='wx_me'):
    manifest = {'kind':'manifest','source':source,'account_name':'我的微信', 'self_id':self_id,
        'contacts':[{'id':'friend','name':'朋友'}], 'sessions':[{'id':'friend','name':'朋友'}]}
    result = upload(client, config, manifest)
    assert result.status_code == 200, result.text
    batch = {'kind':'messages','source':source,'session':'friend','items':[
        {'id':'m:1','database':'message/message_0.db','local_id':1,'timestamp':timestamp,'text':'只属于 Alice 的秘密内容','sender':'我','is_me':True},
        {'id':'m:2','database':'message/message_0.db','local_id':2,'timestamp':timestamp+1,'text':'明天见','sender':'朋友'}]}
    assert upload(client, config, batch).status_code == 200
    assert upload(client, config, {'kind':'history','source':source,'session':'friend','history_complete':True}).status_code == 200
    return Platform.session_id(result.json()['account_id'], 'friend'), batch


def key(client, permission='read', **kwargs):
    result = client.post('/api/v2/keys', headers=UI, json={'name':'测试 '+permission,'permission':permission,**kwargs})
    assert result.status_code == 200, result.text
    return result.json()


def mcp(client, value, tool, arguments=None):
    response = client.post('/mcp', headers={'Authorization':'Bearer '+value,'Accept':'application/json, text/event-stream'},
        json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':tool,'arguments':arguments or {}}})
    return response


def test_login_cookie_origin_roles_and_password_reset(platform):
    admin, service, app = platform
    client, user_id = user(platform)
    assert client.post('/api/v2/clients', json={'name':'bad'}).status_code == 403
    assert client.post('/api/v2/clients', headers={**UI,'Origin':'https://evil.example'}, json={'name':'bad'}).status_code == 403
    assert client.get('/api/v2/manage/users?admin=true').status_code == 403
    assert client.get('/api/v2/overview?admin=true').status_code == 403
    assert client.get('/healthz', headers={'Host':'evil.example'}).status_code == 403
    cookie = admin.cookies.get('wechat_session')
    assert cookie and 'admin' not in cookie
    r = admin.patch('/api/v2/admin/users/'+user_id, headers=UI, json={'password':'Reset-password-123'})
    assert r.status_code == 200
    assert client.get('/api/v2/overview').status_code == 401


def test_registration_binding_rotation_and_user_disable(platform):
    admin, _, _ = platform
    client, user_id = user(platform)
    config = device(client)
    r = client.post('/api/v2/client/register', json={'app_key':config['app_key'],'app_secret':config['app_secret'],'device_id':'different-device-00002'})
    assert r.status_code == 409
    headers = {'Authorization':'Bearer '+config['token']}
    assert client.get('/api/v2/client/status', headers=headers).status_code == 200
    assert client.post(f"/api/v2/clients/{config['id']}/rotate", headers=UI, json={}).status_code == 200
    assert client.get('/api/v2/client/status', headers=headers).status_code == 401
    second = device(client)
    read = key(client)
    admin.patch('/api/v2/admin/users/'+user_id, headers=UI, json={'enabled':False})
    assert client.get('/api/v2/overview').status_code == 401
    assert client.get('/api/v2/client/status', headers={'Authorization':'Bearer '+second['token']}).status_code == 401
    assert mcp(client, read['key'], 'wechat_status').status_code == 401


def test_encrypted_ingestion_deduplication_isolation_and_tamper(platform):
    admin, service, _ = platform
    alice, user_id = user(platform)
    bob, _ = user(platform, 'bob')
    config = device(alice)
    sid, batch = seed(alice, config)
    assert upload(alice, config, batch).json()['changed'] == 0
    assert alice.get('/api/v2/sessions').json()['items'][0]['message_count'] == 2
    assert bob.get('/api/v2/sessions').json()['total'] == 0
    assert bob.get(f'/api/v2/sessions/{sid}/messages').status_code == 404
    assert admin.get(f'/api/v2/sessions/{sid}/messages').status_code == 404
    assert alice.get(f'/api/v2/sessions/{sid}/messages?q=秘密').json()['total'] == 1
    evidence=alice.get(f'/api/v2/sessions/{sid}/messages').json()['items'][0]['evidence_id']
    assert alice.get('/api/v2/messages/'+evidence).json()['session_id']==sid
    assert bob.get('/api/v2/messages/'+evidence).status_code==404
    with service.db.transaction() as con:
        raw = con.execute(select(messages.c.payload)).scalars().all()
        assert all('秘密' not in value for value in raw)
    encrypted = envelope(config, batch)
    assert '秘密' not in json.dumps(encrypted)
    headers = {'Authorization':'Bearer '+config['token']}
    r = alice.post('/api/v2/client/sync', headers=headers, json=encrypted)
    assert r.status_code == 200
    assert alice.post('/api/v2/client/sync', headers=headers, json=encrypted).json() == r.json()
    encrypted['tag'] = 'A'*22
    assert alice.post('/api/v2/client/sync', headers=headers, json=encrypted).status_code == 422
    config_bob = device(bob)
    sid_bob, _ = seed(bob, config_bob)
    assert sid_bob != sid
    assert bob.get('/api/v2/contacts').json()['total'] == 1


def test_two_clients_with_same_source_keep_messages_and_cursors_independent(platform):
    client, user_id = user(platform)
    first, second = device(client), device(client)
    source = 'a' * 64
    manifest = {'kind': 'manifest', 'source': source, 'account_name': '同一微信',
                'self_id': 'wx_me', 'sessions': [{'id': 'friend', 'name': '朋友'}]}
    for config in (first, second):
        assert upload(client, config, manifest).status_code == 200
    def batch(text, local_id):
        return {'kind': 'messages', 'source': source, 'session': 'friend', 'items': [
            {'id': f'db:{local_id}', 'database': 'message/message_0.db', 'local_id': local_id,
             'server_id': 123, 'timestamp': 1704067200, 'text': text}]}
    assert upload(client, first, batch('第一台电脑', 1)).status_code == 200
    assert upload(client, second, batch('第二台电脑', 9)).status_code == 200
    assert upload(client, first, batch('第一台电脑', 1)).json()['changed'] == 0
    listings = client.get('/api/v2/sessions').json()['items']
    assert len(listings) == 1 and listings[0]['message_count'] == 1
    assert client.get(f"/api/v2/sessions/{listings[0]['id']}/messages").json()['items'][0]['text'] == '第一台电脑'
    _, service, _ = platform
    with service.db.transaction() as con:
        copies = list(con.execute(select(messages).where(messages.c.user_id == user_id)).mappings())
        assert {service.decrypt_row('messages', item)['text'] for item in copies} == {'第一台电脑', '第二台电脑'}
    for config, cursor in ((first, 1), (second, 9)):
        response = client.get('/api/v2/client/cursors',
            headers={'Authorization': 'Bearer ' + config['token']}, params={'source': source, 'session': 'friend'})
        assert response.status_code == 200
        assert list(response.json()['cursors'].values()) == [cursor]


def test_priority_sessions_follow_listening_and_client_account_scope(platform):
    alice, _ = user(platform)
    bob, _ = user(platform, 'bob')
    first, second, other_user = device(alice), device(alice), device(bob)
    sid, _ = seed(alice, first)
    headers = lambda config: {'Authorization': 'Bearer ' + config['token']}
    path, params = '/api/v2/client/priority-sessions', {'source': 'a'*64}
    assert alice.get(path, params=params).status_code == 401
    assert alice.get(path, headers=headers(first), params=params).json() == {'sessions': []}
    assert alice.post(f'/api/v2/sessions/{sid}/listener', headers=UI, json={'enabled': True}).status_code == 200
    assert alice.get(path, headers=headers(first), params=params).json() == {'sessions': ['friend']}
    assert alice.get(path, headers=headers(second), params=params).json() == {'sessions': []}
    manifest = {'kind':'manifest','source':'a'*64,'account_name':'同一微信','self_id':'wx_me',
                'sessions':[{'id':'friend','name':'朋友'}]}
    second_account = upload(alice, second, manifest).json()['account_id']
    assert alice.get(path, headers=headers(second), params=params).json() == {'sessions': ['friend']}
    # Both devices expose one logical session while their source rows stay separate.
    assert len(alice.get('/api/v2/sessions').json()['items']) == 1
    _, service, _ = platform
    with service.db.transaction() as con:
        second_session = row(con, sessions, sessions.c.account_id == second_account)
        assert not second_session['listening']
    assert upload(bob, other_user, manifest).status_code == 200
    assert bob.get(path, headers=headers(other_user), params=params).json() == {'sessions': []}
    different = {**manifest, 'source':'b'*64, 'self_id':'another_wechat'}
    assert upload(alice, first, different).status_code == 200
    assert alice.get(path, headers=headers(first), params={'source': 'b'*64}).json() == {'sessions': []}
    assert alice.post(f'/api/v2/sessions/{sid}/listener', headers=UI, json={'enabled': False}).status_code == 200
    assert alice.get(path, headers=headers(first), params=params).json() == {'sessions': []}
    assert alice.get(path, headers=headers(second), params=params).json() == {'sessions': []}


def test_listener_waits_without_model_and_all_periods_are_saved(platform, monkeypatch):
    client, user_id = user(platform)
    _, service, app = platform
    config = device(client)
    sid, _ = seed(client, config)
    r = client.post(f'/api/v2/sessions/{sid}/listener', headers=UI, json={'enabled':True})
    assert r.status_code == 200, r.text
    worker = Worker(service)
    for _ in range(6):
        worker.run_once()
    assert {j['state'] for j in client.get('/api/v2/manage/jobs').json()['items']} == {'waiting_model'}
    assert client.get('/api/v2/model').json() == {'configured':False,'enabled':False}
    monkeypatch.setenv('WECHAT_MODEL_ALLOW_PRIVATE', '1')
    service.save_model(user_id, ModelConfig(base_url='http://127.0.0.1:9999/v1', model='test-user-model'))
    calls = []
    def model(config, prompt, purpose, before_call=None):
        if before_call:
            before_call()
        calls.append(prompt)
        if '仅输出 JSON' in purpose:
            data = json.loads(prompt); me = next(e['id'] for e in data['可用证据'] if e['is_me'])
            return json.dumps({'relationship':[{'text':'与朋友约定见面','evidence':[me]}], 'profile':[{'text':'约定见面','evidence':[me]}]}), {'prompt_tokens':10,'completion_tokens':12}
        return '# 自动记忆\n\n明天与朋友见面。', {'prompt_tokens':10,'completion_tokens':12}
    monkeypatch.setattr('backend.platform_jobs.call_model', model)
    for _ in range(6):
        worker.run_once()
    task_data = client.get('/api/v2/manage/jobs').json()['items']
    assert len(task_data) == 5 and all(j['state'] == 'done' for j in task_data), task_data
    items = client.get('/api/v2/memories').json()['items']
    assert {item['kind'] for item in items} == {'day','week','month','halfyear','year','profile','relationship'}
    memory = client.get('/api/v2/memories/'+items[0]['id']).json()
    assert memory['evidence'] and memory['markdown']
    exported=client.get('/api/v2/memories/'+memory['id']+'/download')
    assert exported.status_code==200 and memory['evidence'][0]['id'] not in exported.text
    assert 'source_sessions:' not in exported.text and exported.text.strip() == memory['markdown'].strip()
    assert len(service.documents.data) == 7 and calls
    assert all('明天' not in ciphertext for ciphertext in service.documents.data.values())


def test_mcp_read_full_scope_revocation_and_manual_versioning(platform):
    admin, service, _ = platform
    client, user_id = user(platform)
    config = device(client)
    sid, _ = seed(client, config)
    read, full = key(client), key(client, 'full')
    assert full['permission'] == 'full' and full['key'].startswith('mcp_full_')
    assert mcp(client, read['key'], 'read_messages', {'session_id':sid}).json()['result']['isError']
    client.post(f'/api/v2/sessions/{sid}/listener', headers=UI, json={'enabled':True})
    result = mcp(client, read['key'], 'read_messages', {'session_id':sid})
    assert result.status_code == 200 and not result.json()['result'].get('isError'), result.text
    for value in (read['key'], full['key']):
        for tool, arguments in [('list_sessions', {}), ('read_messages', {'session_id':sid}),
                ('search_messages', {'session_id':sid, 'query':'秘密'}), ('suggest_reply', {'session_id':sid})]:
            assert not mcp(client, value, tool, arguments).json()['result'].get('isError'), tool
        principal = service.authenticate(value, 'mcp')
        evidence_id = service.read_messages(principal, sid)['items'][0]['evidence_id']
        assert not mcp(client, value, 'get_message', {'evidence_id':evidence_id}).json()['result'].get('isError')
    args = {'session_id':sid, 'markdown':'# 手动记忆\n不要覆盖', 'request_id':'request-00001'}
    assert mcp(client, read['key'], 'create_memory', args).json()['result']['isError']
    result = mcp(client, full['key'], 'create_memory', args).json()['result']
    assert not result.get('isError'), result
    assert not mcp(client, full['key'], 'create_memory', args).json()['result'].get('isError')
    item = client.get('/api/v2/memories').json()['items'][0]
    memory_id = item['id']
    for value in (read['key'], full['key']):
        assert not mcp(client, value, 'list_memories').json()['result'].get('isError')
        assert not mcp(client, value, 'read_memory', {'memory_id':memory_id}).json()['result'].get('isError')
    edit = {'memory_id': memory_id, 'version': 1, 'markdown': '# MCP 修订'}
    assert mcp(client, read['key'], 'update_memory', edit).json()['result']['isError']
    assert not mcp(client, full['key'], 'update_memory', edit).json()['result'].get('isError')
    assert mcp(client, full['key'], 'update_memory', edit).json()['result']['isError']
    assert client.put('/api/v2/memories/'+memory_id, headers=UI, json={'version':2,'markdown':'# 修订'}).status_code == 200
    assert client.put('/api/v2/memories/'+memory_id, headers=UI, json={'version':1,'markdown':'# 冲突'}).status_code == 409
    with service.db.transaction() as con:
        service.publish_memory(con,user_id,memory_id,sid,'manual',{'markdown':'自动版','evidence':[]},manual=False)
    result = client.get('/api/v2/memories/'+memory_id).json()
    assert result['markdown'] == '# 修订' and result['latest_version'] == 4
    assert client.get('/api/v2/memories/'+memory_id+'?version=4').json()['markdown'] == '自动版'
    assert admin.post(f"/api/v2/keys/{read['id']}/enabled?admin=true", headers=UI, json={'enabled':False}).status_code == 200
    assert mcp(client, read['key'], 'read_messages', {'session_id':sid}).status_code == 401
    assert mcp(client, read['key'], 'set_listener', {'session_id':sid, 'enabled':False}).status_code == 401
    other_read = key(client)
    assert mcp(client, other_read['key'], 'set_listener', {'session_id':sid, 'enabled':False}).json()['result']['isError']
    assert not mcp(client, full['key'], 'set_listener', {'session_id':sid, 'enabled':False}).json()['result'].get('isError')
    for value in (other_read['key'], full['key']):
        assert mcp(client, value, 'read_messages', {'session_id':sid}).json()['result']['isError']
    assert mcp(client, full['key'], 'create_memory', {**args, 'request_id':'request-00002'}).json()['result']['isError']


def test_period_boundaries_leap_year_week_and_halfyear():
    ts = int(datetime(2024,2,29,12,tzinfo=ZONE).timestamp())
    assert period(ts,'day')[1]-period(ts,'day')[0] == 86400
    assert period(ts,'month')[1]-period(ts,'month')[0] == 29*86400
    start,end = period(int(datetime(2026,1,1,tzinfo=ZONE).timestamp()),'week')
    assert datetime.fromtimestamp(start,ZONE).isoformat() == '2025-12-29T00:00:00+08:00'
    assert end-start == 7*86400
    start,end = period(ts,'halfyear')
    assert datetime.fromtimestamp(end,ZONE).isoformat() == '2024-07-01T00:00:00+08:00'


def test_worker_cannot_commit_after_listener_disabled(platform, monkeypatch):
    client, user_id = user(platform)
    _, service, _ = platform
    config=device(client);sid,_=seed(client,config)
    client.post(f'/api/v2/sessions/{sid}/listener', headers=UI, json={'enabled':True,'cycles':['day']})
    monkeypatch.setenv('WECHAT_MODEL_ALLOW_PRIVATE', '1')
    service.save_model(user_id,ModelConfig(base_url='http://127.0.0.1:9999/v1',model='test'))
    def model(config,prompt,purpose,before_call=None):
        if before_call:before_call()
        client.post(f'/api/v2/sessions/{sid}/listener', headers=UI, json={'enabled':False,'cycles':['day']})
        return '不得保存',{}
    monkeypatch.setattr('backend.platform_jobs.call_model',model)
    Worker(service).run_once()
    assert client.get('/api/v2/memories').json()['total']==0
    assert client.get('/api/v2/manage/jobs').json()['items'][0]['state']=='cancelled'


def test_mongo_failure_never_publishes_a_dangling_pointer(platform, monkeypatch):
    client,user_id=user(platform);_,service,_=platform
    config=device(client);sid,_=seed(client,config)
    client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True})
    principal={'user_id':user_id,'role':'user','kind':'web'}
    def failed(*args):raise OSError('Mongo unavailable')
    monkeypatch.setattr(service.documents,'put',failed)
    with pytest.raises(OSError):
        service.create_memory(principal,sid,'# 不应发布','request-1234')
    with service.db.transaction() as con:
        assert con.execute(select(func.count()).select_from(memories)).scalar_one()==0
        assert con.execute(select(func.count()).select_from(versions)).scalar_one()==0


def test_expired_lease_recovers_and_future_period_cannot_be_forced(platform,monkeypatch):
    client,user_id=user(platform);_,service,_=platform
    config=device(client);sid,_=seed(client,config)
    client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True,'cycles':['day']})
    monkeypatch.setenv('WECHAT_MODEL_ALLOW_PRIVATE','1')
    service.save_model(user_id,ModelConfig(base_url='http://127.0.0.1:9999/v1',model='test'))
    worker=Worker(service)
    claimed=worker.claim()
    assert claimed
    with service.db.transaction() as con:
        con.execute(jobs.update().where(jobs.c.id==claimed['id']).values(lease_until=0))
    monkeypatch.setattr('backend.platform_jobs.call_model',lambda config,prompt,purpose,before_call=None:
        ('{"relationship":[],"profile":[]}' if '仅输出 JSON' in purpose else '# 恢复成功',{}))
    assert Worker(service).run_once()
    assert client.get('/api/v2/manage/jobs').json()['items'][0]['state']=='done'
    seed(client,config,timestamp=int(time.time()))
    tasks=client.get('/api/v2/manage/jobs').json()['items']
    future=next(j for j in tasks if j['period_end']>time.time())
    assert client.post(f"/api/v2/jobs/{future['id']}/retry",headers=UI,json={}).status_code==200
    with service.db.transaction() as con:
        stored=row(con,jobs,jobs.c.id==future['id'])
        assert stored['next_run']>=stored['period_end']


@pytest.mark.parametrize('permission', ['read', 'full'])
def test_scoped_mcp_key_and_rotation_do_not_expand_access(platform, permission):
    client,user_id=user(platform);_,service,_=platform
    config=device(client);sid,_=seed(client,config)
    other,_=seed(client,config,source='b'*64,self_id='another_wechat')
    all_sessions=client.get('/api/v2/sessions').json()['items']
    assert len(all_sessions)==2
    for item in all_sessions:
        client.post(f"/api/v2/sessions/{item['id']}/listener",headers=UI,json={'enabled':True})
    selected=all_sessions[0]['id'];excluded=all_sessions[1]['id']
    read=key(client,permission,sessions=[selected])
    assert not mcp(client,read['key'],'read_messages',{'session_id':selected}).json()['result'].get('isError')
    assert mcp(client,read['key'],'read_messages',{'session_id':excluded}).json()['result']['isError']
    assert mcp(client,read['key'],'create_memory',{'session_id':excluded,'markdown':'越界','request_id':'scope-excluded'}).json()['result']['isError']
    rotated=client.post(f"/api/v2/keys/{read['id']}/rotate",headers=UI,json={})
    assert rotated.status_code==200
    assert mcp(client,read['key'],'wechat_status').status_code==401
    assert not mcp(client,rotated.json()['key'],'read_messages',{'session_id':selected}).json()['result'].get('isError')
    assert mcp(client,rotated.json()['key'],'read_messages',{'session_id':excluded}).json()['result']['isError']


def test_mcp_permission_values_and_legacy_write_migration(platform, monkeypatch):
    from backend import platform_db
    monkeypatch.setattr(platform_db, 'SQL_MIGRATIONS', platform_db.SQL_MIGRATIONS[:2])
    client, user_id = user(platform)
    _, service, _ = platform
    sid, _ = seed(client, device(client))
    client.post(f'/api/v2/sessions/{sid}/listener', headers=UI, json={'enabled':True})
    legacy = key(client, 'write', sessions=[sid], expires_days=12)
    assert legacy['permission'] == 'full'
    assert client.post('/api/v2/keys', headers=UI, json={'name':'invalid','permission':'admin'}).status_code == 422
    legacy_token = 'mcp_write_' + secrets.token_urlsafe(32)
    with service.db.transaction() as con:
        con.execute(mcp_keys.update().where(mcp_keys.c.id == legacy['id']).values(
            permission='write', hash=digest(legacy_token), prefix=legacy_token[:16]))
        con.execute(delete(schema).where(schema.c.version >= 2))
        before = row(con, mcp_keys, mcp_keys.c.id == legacy['id'])
    database = Database(str(service.db.engine.url))
    try:
        with database.transaction() as con:
            after = row(con, mcp_keys, mcp_keys.c.id == legacy['id'])
            assert after == {**before, 'permission':'full'}
            assert con.execute(select(schema.c.version).where(schema.c.version == 2)).scalar_one() == 2
    finally:
        database.engine.dispose()
    # A second startup must be harmless; the old token and scope remain usable.
    database = Database(str(service.db.engine.url))
    database.engine.dispose()
    assert service.authenticate(legacy_token, 'mcp')['key']['permission'] == 'full'
    assert not mcp(client, legacy_token, 'read_messages', {'session_id':sid}).json()['result'].get('isError')
    assert not mcp(client, legacy_token, 'create_memory', {'session_id':sid,'markdown':'# 升级后','request_id':'legacy-upgrade'}).json()['result'].get('isError')
    listing = client.get('/api/v2/manage/keys').json()['items']
    assert next(item for item in listing if item['id'] == legacy['id'])['permission'] == 'full'
    rotated = client.post(f"/api/v2/keys/{legacy['id']}/rotate", headers=UI, json={}).json()
    assert rotated['key'].startswith('mcp_full_')
    assert mcp(client, legacy_token, 'wechat_status').status_code == 401
    assert not mcp(client, rotated['key'], 'read_messages', {'session_id':sid}).json()['result'].get('isError')
    with service.db.transaction() as con:
        con.execute(mcp_keys.update().where(mcp_keys.c.id == legacy['id']).values(expires=0))
    assert mcp(client, rotated['key'], 'wechat_status').status_code == 401


def test_full_key_profile_and_user_boundaries(platform):
    alice, user_id = user(platform)
    bob, _ = user(platform, 'bob')
    _, service, _ = platform
    sid, _ = seed(alice, device(alice))
    bob_sid, _ = seed(bob, device(bob))
    for client, session_id in ((alice, sid), (bob, bob_sid)):
        client.post(f'/api/v2/sessions/{session_id}/listener', headers=UI, json={'enabled':True})
    with service.db.transaction() as con:
        service.publish_memory(con, user_id, 'profile-test', '', 'profile', {
            'markdown':'# 画像','evidence':[], 'source_sessions':[sid]})
    full = key(alice, 'full', profile=True)
    for tool, args in [('read_messages', {'session_id':bob_sid}),
            ('create_memory', {'session_id':bob_sid,'markdown':'no','request_id':'cross-user-test'})]:
        assert mcp(alice, full['key'], tool, args).json()['result']['isError']
    assert not mcp(alice, full['key'], 'read_memory', {'memory_id':'profile-test'}).json()['result'].get('isError')
    assert not mcp(alice, full['key'], 'update_memory', {'memory_id':'profile-test','version':1,'markdown':'# 修订画像'}).json()['result'].get('isError')
    restricted = [key(alice, 'full'), key(alice, 'full', profile=True, sessions=[sid])]
    for item in restricted:
        for tool, args in [('read_memory', {'memory_id':'profile-test'}),
                ('update_memory', {'memory_id':'profile-test','version':2,'markdown':'no'})]:
            assert mcp(alice, item['key'], tool, args).json()['result']['isError']
    assert alice.post(f"/api/v2/keys/{full['id']}/revoke", headers=UI, json={}).status_code == 200
    assert mcp(alice, full['key'], 'wechat_status').status_code == 401
