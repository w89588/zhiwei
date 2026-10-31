"""Listening workspace contracts, stable message cursors and lazy memory reads."""
from sqlalchemy import event, select
from fastapi.testclient import TestClient

from backend.platform_db import contacts, jobs, memories, sessions, row
from backend.platform_service import uid, now
from test_platform import platform, user, device, seed, upload, UI


def test_favicon_is_public_and_supports_head_before_login(platform):
    anonymous = TestClient(platform[2])
    response = anonymous.get('/favicon.ico')
    assert response.status_code == 200 and response.content[:4] == b'\x00\x00\x01\x00'
    assert response.headers['content-type'] == 'image/vnd.microsoft.icon'
    assert 'max-age' in response.headers['cache-control']
    head = anonymous.head('/favicon.ico')
    assert head.status_code == 200 and not head.content
    assert head.headers['content-length'] == str(len(response.content))
    assert '/favicon.ico?v=' in anonymous.get('/').text


def test_listening_contacts_types_search_and_add_without_conversation(platform):
    client, user_id = user(platform)
    bob, _ = user(platform, 'bob')
    _, service, _ = platform
    config = device(client); sid, _ = seed(client, config)
    result = upload(client, config, {'kind':'manifest','source':'a'*64,'contacts':[
        {'id':'group@chatroom','name':'项目组','remark':'项目 A','group':True}], 'sessions':[]})
    assert result.status_code == 200
    assert client.get('/api/v2/workspace/contacts').json()['total'] == 0
    all_people = client.get('/api/v2/workspace/contacts?scope=all').json()['items']
    group = next(c for c in all_people if c['group'])
    assert bob.post(f"/api/v2/workspace/contacts/{group['id']}/listener", headers=UI, json={'enabled':True}).status_code == 404
    assert client.post(f"/api/v2/workspace/contacts/{group['id']}/listener", headers=UI, json={'enabled':True}).status_code == 200
    data = client.get('/api/v2/workspace/contacts?contact_type=group&q=项目').json()
    assert data['total'] == 1 and data['items'][0]['id'] == group['id']
    assert client.get('/api/v2/workspace/contacts?contact_type=person').json()['total'] == 0
    assert client.post(f"/api/v2/workspace/contacts/{group['id']}/listener", headers=UI, json={'enabled':True}).status_code == 200
    client.post(f"/api/v2/workspace/contacts/{group['id']}/listener", headers=UI, json={'enabled':False})
    assert client.get('/api/v2/workspace/contacts').json()['total'] == 0
    assert client.get('/api/v2/workspace/contacts?scope=all').json()['total'] == 2


def test_message_cursor_history_live_append_and_user_boundaries(platform):
    client, _ = user(platform); bob, _ = user(platform, 'bob')
    config = device(client); sid, _ = seed(client, config)
    first = client.get(f'/api/v2/sessions/{sid}/messages?limit=1&count=false').json()
    assert len(first['items']) == 1 and first['has_more'] and first['total'] is None
    older = client.get(f"/api/v2/sessions/{sid}/messages", params={'before':first['older_cursor'],'limit':1,'count':False}).json()
    assert older['items'][0]['evidence_id'] != first['items'][0]['evidence_id']
    # Equal timestamps still use the stable evidence-id tie breaker.
    assert client.get(f'/api/v2/sessions/{sid}/messages', params={'after':first['newer_cursor'],'count':False}).json()['items'] == []
    upload(client, config, {'kind':'messages','source':'a'*64,'session':'friend','items':[
        {'id':'m:3','database':'message/message_0.db','local_id':3,'timestamp':1704067300,'text':'新消息','sender':'朋友'}]})
    newer = client.get(f'/api/v2/sessions/{sid}/messages', params={'after':first['newer_cursor'],'count':False}).json()
    assert [m['text'] for m in newer['items']] == ['新消息']
    assert bob.get(f'/api/v2/sessions/{sid}/messages', params={'after':first['newer_cursor']}).status_code == 404
    assert client.get(f'/api/v2/sessions/{sid}/messages?before=bad').status_code == 422


def test_directory_has_no_body_reads_and_only_listening_owned_objects(platform, monkeypatch):
    client, user_id = user(platform); config=device(client); sid,_=seed(client,config)
    _,service,_=platform
    seed(client,config,source='b'*64,self_id='another_wechat')
    other=next(s['id'] for s in client.get('/api/v2/sessions').json()['items'] if s['id'] != sid)
    client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True})
    with service.db.transaction() as con:
        for mid, session in [('listened',sid),('hidden',other),('personal','')]:
            service.publish_memory(con,user_id,mid,session,'manual' if session else 'profile',
                {'markdown':'# 正文','evidence':[],'source_sessions':[sid]},manual=True)
    monkeypatch.setattr(service.documents,'get',lambda *a: (_ for _ in ()).throw(AssertionError('directory must not load bodies')))
    result=client.get('/api/v2/workspace/memories')
    assert result.status_code==200
    names={d['id'] for folder in result.json()['items'] for docs in folder['types'].values() for d in docs}
    assert names=={'listened','personal'}


def test_session_count_query_is_bounded_by_page_not_number_of_sessions(platform):
    client, user_id=user(platform); config=device(client); seed(client,config)
    _,service,_=platform
    upload(client,config,{'kind':'manifest','source':'a'*64,'sessions':[{'id':f'friend{i}','name':f'朋友{i}'} for i in range(25)]})
    statements=[]
    def record(conn,cursor,statement,*args): statements.append(statement)
    event.listen(service.db.engine,'before_cursor_execute',record)
    try:
        result=client.get('/api/v2/sessions?limit=20')
    finally:
        event.remove(service.db.engine,'before_cursor_execute',record)
    assert result.status_code==200 and len(result.json()['items'])==20
    assert len([s for s in statements if 'count(' in s.lower() and 'v2_message_pool' in s.lower()])==1


def test_manifest_noop_does_not_reencrypt_and_analysis_groups_history(platform):
    client,user_id=user(platform); config=device(client);sid,_=seed(client,config)
    _,service,_=platform
    with service.db.transaction() as con: before=row(con,sessions,sessions.c.id==sid)['payload']
    upload(client,config,{'kind':'manifest','source':'a'*64,'sessions':[{'id':'friend','name':'朋友'}]})
    with service.db.transaction() as con: assert row(con,sessions,sessions.c.id==sid)['payload']==before
    client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True})
    response=client.get('/api/v2/workspace/analysis').json()
    assert response['total']==1 and len(response['items'])==1
    assert sum(response['items'][0]['counts'].values()) >= 5
    upload(client,config,{'kind':'messages','source':'a'*64,'session':'friend','items':[
        {'id':'m:3','database':'message/message_0.db','local_id':3,'timestamp':1704067205,'text':'迟到的历史','sender':'我'}]})
    with service.db.transaction() as con:
        queued=con.execute(select(jobs).where(jobs.c.user_id==user_id)).mappings().all()
        assert all(j['next_run'] >= now()+40 for j in queued)
