import json

from sqlalchemy import delete, select

from backend.platform_db import accounts, history_sync, jobs, logical_sessions, memories, message_pool, messages, row, sessions
from backend.platform_jobs import Worker
from backend.platform_models import ModelConfig
from test_platform import platform, user, device, key, mcp, upload, UI


def batch(source, values):
    return {'kind':'messages','source':source,'session':'friend','items':[
        {'id':f'local:{local}', 'database':'message/message_0.db','local_id':local,'server_id':server,
         'timestamp':1704067200+local,'text':text,'sender':'朋友','is_me':False}
        for local,server,text in values]}


def test_same_wechat_has_one_account_listener_and_deduplicated_pool(platform, monkeypatch):
    alice, user_id = user(platform); bob, _ = user(platform, 'bob'); _, service, _ = platform
    one, two, outsider = device(alice), device(alice), device(bob)
    manifest={'kind':'manifest','source':'a'*64,'account_name':'微信','self_id':'wx_me',
              'contacts':[{'id':'friend','name':'朋友'}], 'sessions':[{'id':'friend','name':'朋友'}]}
    assert upload(alice,one,manifest).status_code == 200
    first=upload(alice,one,batch('a'*64,[(1,'shared-100','第一台内容'),(2,'only-1','第一台独有')]))
    assert first.status_code == 200
    sid=alice.get('/api/v2/sessions').json()['items'][0]['id']
    assert upload(alice,one,{'kind':'history','source':'a'*64,'session':'friend','history_complete':True}).status_code == 200
    assert alice.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True,'cycles':['day']}).status_code==200
    assert upload(alice,two,manifest).status_code == 200
    assert upload(alice,two,{**manifest,'sessions':[{'id':'friend','name':'朋友','timestamp':1704067300,'preview':'较新预览'}]}).status_code == 200
    assert upload(alice,two,batch('a'*64,[(11,'shared-100','第二台不同内容'),(12,'only-2','第二台独有')])).status_code == 200
    assert upload(alice,two,{'kind':'history','source':'a'*64,'session':'friend','history_complete':True}).status_code == 200
    # Same identity for a different platform user must never merge.
    assert upload(bob,outsider,manifest).status_code == 200
    assert upload(bob,outsider,batch('a'*64,[(1,'shared-100','另一个用户')])).status_code == 200
    account_list=alice.get('/api/v2/accounts').json()
    assert account_list['total']==1 and account_list['items'][0]['client_count']==2
    overview=alice.get('/api/v2/overview').json()
    assert overview['accounts']==1 and overview['contacts']==1 and overview['sessions']==1 and overview['listening']==1
    for account in (account_list['items'][0]['id'], first.json()['account_id']):
        assert alice.get('/api/v2/workspace/contacts',params={'scope':'all','account_id':account}).json()['total']==1
        assert alice.get('/api/v2/sessions',params={'account_id':account}).json()['total']==1
    people=alice.get('/api/v2/workspace/contacts?scope=all').json()
    assert people['total']==1 and people['items'][0]['listening']
    result=alice.get('/api/v2/sessions').json()
    assert result['total']==1 and result['items'][0]['id']==sid and result['items'][0]['message_count']==3
    assert result['items'][0]['preview']=='较新预览'
    items=alice.get(f'/api/v2/sessions/{sid}/messages').json()['items']
    assert len(items)==3 and {x['text'] for x in items}=={'第一台内容','第一台独有','第二台独有'}
    unique_other = next(item for item in items if item['text']=='第二台独有')
    scoped=key(alice,'read',sessions=[sid])
    assert not mcp(alice,scoped['key'],'get_message',{'evidence_id':unique_other['evidence_id']}).json()['result'].get('isError')
    assert mcp(bob,key(bob,'read')['key'],'get_message',{'evidence_id':unique_other['evidence_id']}).json()['result']['isError']
    assert bob.get('/api/v2/sessions').json()['total']==1 and bob.get(f'/api/v2/sessions/{sid}/messages').status_code==404
    with service.db.transaction() as con:
        physical=list(con.execute(select(sessions).where(sessions.c.user_id==user_id)).mappings())
        assert len(physical)==2 and sum(bool(x['listening']) for x in physical)==1
        assert len(list(con.execute(select(messages).where(messages.c.user_id==user_id)).mappings()))==4
        assert len(list(con.execute(select(message_pool).where(message_pool.c.user_id==user_id)).mappings()))==3
    for config,expected in ((one,2),(two,12)):
        response=alice.get('/api/v2/client/cursors',headers={'Authorization':'Bearer '+config['token']},
            params={'source':'a'*64,'session':'friend'}).json()
        assert list(response['cursors'].values())==[expected]
    assert alice.get('/api/v2/workspace/analysis').json()['total']==1
    alias=next(x['id'] for x in physical if x['id']!=sid)
    alias_key=key(alice,'read',sessions=[alias])
    alias_list=mcp(alice,alias_key['key'],'list_sessions').json()['result']
    assert not alias_list.get('isError')
    assert not mcp(alice,alias_key['key'],'read_messages',{'session_id':sid}).json()['result'].get('isError')
    assert alice.post(f'/api/v2/sessions/{alias}/listener',headers=UI,json={'enabled':False,'cycles':['day']}).status_code==200
    assert alice.get('/api/v2/workspace/contacts').json()['total']==0
    for config in (one,two):
        response=alice.get('/api/v2/client/priority-sessions',headers={'Authorization':'Bearer '+config['token']},params={'source':'a'*64})
        assert response.json()=={'sessions':[]}


def test_distinct_wechat_accounts_and_missing_identity_are_not_merged(platform):
    alice,_=user(platform); config=device(alice)
    for source,identity in (('a'*64,'wx_one'),('b'*64,'wx_two'),('c'*64,''),('d'*64,'')):
        assert upload(alice,config,{'kind':'manifest','source':source,'self_id':identity,'account_name':'微信',
            'sessions':[{'id':'friend','name':'朋友'}],'contacts':[{'id':'friend','name':'朋友'}]}).status_code==200
    assert alice.get('/api/v2/accounts').json()['total']==4
    assert alice.get('/api/v2/sessions').json()['total']==4
    assert alice.get('/api/v2/workspace/contacts?scope=all').json()['total']==4


def test_missing_server_message_identifier_never_guesses_duplicate(platform):
    client,_=user(platform);first,second=device(client),device(client)
    manifest={'kind':'manifest','source':'a'*64,'self_id':'wx_me','sessions':[{'id':'friend','name':'朋友'}]}
    for config in (first,second):
        upload(client,config,manifest)
        payload=batch('a'*64,[(1,0,'相同文字')])
        assert upload(client,config,payload).status_code==200
    items=client.get('/api/v2/sessions').json()['items']
    assert len(items)==1 and items[0]['message_count']==2


def test_analysis_waits_for_both_histories_and_reads_each_message_once(platform, monkeypatch):
    client,user_id=user(platform); _,service,_=platform
    first,second=device(client),device(client)
    manifest={'kind':'manifest','source':'a'*64,'self_id':'wx_me','sessions':[{'id':'friend','name':'朋友'}]}
    upload(client,first,manifest)
    upload(client,first,batch('a'*64,[(1,'shared','唯一一次'),(2,'only-first','第一条独有')]))
    upload(client,first,{'kind':'history','source':'a'*64,'session':'friend','history_complete':True})
    sid=client.get('/api/v2/sessions').json()['items'][0]['id']
    client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True,'cycles':['day']})
    upload(client,second,manifest)
    upload(client,second,batch('a'*64,[(11,'shared','另一副本'),(12,'only-second','第二条独有')]))
    monkeypatch.setenv('WECHAT_MODEL_ALLOW_PRIVATE','1')
    service.save_model(user_id,ModelConfig(base_url='http://127.0.0.1:9999/v1',model='fake'))
    with service.db.transaction() as con:
        con.execute(jobs.update().where(jobs.c.user_id==user_id).values(next_run=0))
    assert Worker(service).claim() is None
    assert upload(client,second,{'kind':'history','source':'a'*64,'session':'friend','history_complete':True}).status_code==200
    with service.db.transaction() as con:
        con.execute(jobs.update().where(jobs.c.user_id==user_id).values(next_run=0))
    inputs=[]
    def fake(config,prompt,purpose,before_call=None):
        if before_call:before_call()
        if '仅输出 JSON' in purpose:return '{"relationship":[],"profile":[]}',{}
        inputs.append(prompt);return '汇总',{}
    monkeypatch.setattr('backend.platform_jobs.call_model',fake)
    assert Worker(service).run_once()
    assert len(inputs)==1 and all(t in inputs[0] for t in ('唯一一次','第一条独有','第二条独有'))
    assert '另一副本' not in inputs[0]
    with service.db.transaction() as con:
        before=dict(con.execute(select(jobs).where(jobs.c.user_id==user_id)).mappings().one())
    # A retransmission of the second device's copy is never a new logical message.
    changed=batch('a'*64,[(11,'shared','第二台的新副本')])
    assert upload(client,second,changed).status_code==200
    with service.db.transaction() as con:
        after=con.execute(select(jobs).where(jobs.c.id==before['id'])).mappings().one()
        assert after['revision']==before['revision'] and after['state']=='done'


def test_late_wechat_identity_merges_existing_pools_without_losing_sources(platform):
    client,user_id=user(platform); _,service,_=platform
    first,second=device(client),device(client)
    base={'kind':'manifest','source':'a'*64,'sessions':[{'id':'friend','name':'朋友'}]}
    assert upload(client,first,{**base,'self_id':''}).status_code==200
    assert upload(client,first,batch('a'*64,[(1,'same','第一份'),(2,'first','第一独有')])).status_code==200
    sid=client.get('/api/v2/sessions').json()['items'][0]['id']
    upload(client,first,{'kind':'history','source':'a'*64,'session':'friend','history_complete':True})
    client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True,'cycles':['day']})
    assert upload(client,second,{**base,'self_id':'wx_me'}).status_code==200
    assert upload(client,second,batch('a'*64,[(11,'same','第二份'),(12,'second','第二独有')])).status_code==200
    assert client.get('/api/v2/accounts').json()['total']==2
    assert upload(client,first,{**base,'self_id':'wx_me'}).status_code==200
    assert client.get('/api/v2/accounts').json()['total']==1
    combined=client.get('/api/v2/sessions').json()
    assert combined['total']==1 and combined['items'][0]['id']==sid and combined['items'][0]['message_count']==3
    with service.db.transaction() as con:
        assert len(list(con.execute(select(messages).where(messages.c.user_id==user_id)).mappings()))==4
        assert len(list(con.execute(select(message_pool).where(message_pool.c.user_id==user_id)).mappings()))==3


def test_legacy_duplicate_listeners_merge_and_preserve_existing_memories(platform):
    client,user_id=user(platform); _,service,_=platform
    first,second=device(client),device(client)
    manifest={'kind':'manifest','source':'a'*64,'self_id':'wx_me','sessions':[{'id':'friend','name':'朋友'}]}
    for config, local in ((first,1),(second,11)):
        upload(client,config,manifest)
        upload(client,config,batch('a'*64,[(local,'same','共同消息')]))
        upload(client,config,{'kind':'history','source':'a'*64,'session':'friend','history_complete':True})
    with service.db.transaction() as con:
        source_rows=[dict(x) for x in con.execute(select(sessions).where(sessions.c.user_id==user_id)).mappings()]
        owner=source_rows[0]['id']; other=source_rows[1]['id']
        con.execute(delete(message_pool).where(message_pool.c.user_id==user_id))
        con.execute(delete(logical_sessions).where(logical_sessions.c.user_id==user_id))
        con.execute(sessions.update().where(sessions.c.id==owner).values(listening=True,cycles='day'))
        con.execute(sessions.update().where(sessions.c.id==other).values(listening=True,cycles='week'))
        service.publish_memory(con,user_id,'retained-owner',owner,'day',{'markdown':'# 保留一','evidence':[]})
        service.publish_memory(con,user_id,'retained-alias',other,'day',{'markdown':'# 保留二','evidence':[]})
        service.publish_memory(con,user_id,'retained-owner-2',owner,'day',{'markdown':'# 保留三','evidence':[]})
    result=client.get('/api/v2/workspace/analysis').json()
    assert result['total']==1 and result['items'][0]['id']==owner
    assert set(result['items'][0]['cycles'])=={'day','week'}
    tree=client.get('/api/v2/workspace/memories').json()
    grouped=[x for x in tree['items'] if x['id'] != 'personal']
    assert len(grouped)==1 and {x['id'] for x in grouped[0]['types']['day']}=={'retained-owner','retained-owner-2','retained-alias'}
    with service.db.transaction() as con:
        source_rows=list(con.execute(select(sessions).where(sessions.c.user_id==user_id)).mappings())
        assert sum(bool(x['listening']) for x in source_rows)==1
        assert len(list(con.execute(select(messages).where(messages.c.user_id==user_id)).mappings()))==2
        assert len(list(con.execute(select(message_pool).where(message_pool.c.user_id==user_id)).mappings()))==1
