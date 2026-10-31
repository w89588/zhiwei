import json
from datetime import datetime, timedelta

from sqlalchemy import select

from backend.platform_db import history_sync, jobs, memories, row
from backend.platform_jobs import Worker, ZONE
from backend.platform_models import ModelConfig
from backend.platform_security import digest
from test_platform import platform, user, device, seed, upload, UI


def test_history_must_be_complete_and_jobs_run_day_then_week(platform, monkeypatch):
    client, user_id = user(platform)
    _, service, _ = platform
    config = device(client)
    monday = int(datetime(2024, 1, 1, tzinfo=ZONE).timestamp())
    sid, _ = seed(client, config, timestamp=monday)
    assert upload(client, config, {'kind':'history','source':'a'*64,'session':'friend','history_complete':False}).status_code == 200
    for offset in (2, 6, 7):
        assert upload(client, config, {'kind':'messages','source':'a'*64,'session':'friend','items':[
            {'id':f'm:{offset+10}','database':'message/message_0.db','local_id':offset+10,
             'timestamp':monday+offset*86400,'text':'合成','sender':'我','is_me':True}]}).status_code == 200
    assert client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True,'cycles':['day','week']}).status_code == 200
    monkeypatch.setenv('WECHAT_MODEL_ALLOW_PRIVATE', '1')
    service.save_model(user_id, ModelConfig(base_url='http://127.0.0.1:9999/v1',model='fake'))
    worker=Worker(service)
    assert worker.claim() is None
    assert {j['state'] for j in client.get('/api/v2/manage/jobs').json()['items']} == {'waiting_history'}
    assert upload(client, config, {'kind':'history','source':'a'*64,'session':'friend','history_complete':True}).status_code == 200
    ordered=[]
    while job := worker.claim():
        ordered.append((job['period_end'], job['cycle']))
        with service.db.transaction() as con:
            con.execute(jobs.update().where(jobs.c.id == job['id']).values(state='done',lease=''))
    assert [cycle for _,cycle in ordered] == ['day','day','day','week','day','week']
    assert [end for end,_ in ordered] == sorted(end for end,_ in ordered)


def test_late_older_data_cancels_inflight_newer_result(platform, monkeypatch):
    from backend.platform_jobs import Cancelled
    client, user_id = user(platform); _, service, _ = platform
    config=device(client)
    sid,_=seed(client,config,timestamp=1704153600)
    client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True,'cycles':['day']})
    monkeypatch.setenv('WECHAT_MODEL_ALLOW_PRIVATE','1')
    service.save_model(user_id,ModelConfig(base_url='http://127.0.0.1:9999/v1',model='fake'))
    worker=Worker(service); current=worker.claim(); assert current
    assert upload(client,config,{'kind':'messages','source':'a'*64,'session':'friend','items':[
        {'id':'m:90','database':'message/message_0.db','local_id':90,'timestamp':1704067200,
         'text':'更早的聊天','sender':'我','is_me':True}]}).status_code == 200
    import pytest
    with pytest.raises(Cancelled): worker.guard(current)


def test_completed_model_output_survives_history_pause(platform, monkeypatch):
    client, user_id = user(platform); _, service, _ = platform
    config=device(client); sid,_=seed(client,config)
    client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True,'cycles':['day']})
    monkeypatch.setenv('WECHAT_MODEL_ALLOW_PRIVATE','1')
    service.save_model(user_id,ModelConfig(base_url='http://127.0.0.1:9999/v1',model='fake'))
    calls=[]
    def model(config_,prompt,purpose,before_call=None):
        if before_call: before_call()
        calls.append(purpose)
        if len(calls)==1:
            for complete in (False,True):
                assert upload(client,config,{'kind':'history','source':'a'*64,'session':'friend',
                    'history_complete':complete}).status_code==200
        return ('{"relationship":[],"profile":[]}' if '仅输出 JSON' in purpose else '摘要'),{}
    monkeypatch.setattr('backend.platform_jobs.call_model',model)
    assert Worker(service).run_once()
    assert Worker(service).run_once()
    assert len(calls)==2  # The successful first call is reused, not billed again.
    assert client.get('/api/v2/manage/jobs').json()['items'][0]['state']=='done'


def test_memory_changes_only_with_relevant_evidence_and_preserves_previous_text(platform, monkeypatch):
    client, user_id = user(platform); _, service, _ = platform
    config = device(client); sid, _ = seed(client, config)
    assert client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True,'cycles':['day']}).status_code == 200
    monkeypatch.setenv('WECHAT_MODEL_ALLOW_PRIVATE', '1')
    service.save_model(user_id, ModelConfig(base_url='http://127.0.0.1:9999/v1',model='fake'))
    decisions = [True, False]
    def model(config, prompt, purpose, before_call=None):
        if before_call: before_call()
        if '仅输出 JSON' not in purpose: return '合成摘要', {}
        data = json.loads(prompt)
        person = next(e for e in data['可用证据'] if e['is_me'])
        update = [{'text':'本人安排见面','evidence':[person['id']]}] if decisions.pop(0) else []
        return json.dumps({'relationship':update,'profile':update}), {}
    monkeypatch.setattr('backend.platform_jobs.call_model',model)
    assert Worker(service).run_once()
    with service.db.transaction() as con:
        saved = {r['kind']:dict(r) for r in con.execute(select(memories)).mappings()}
    assert saved['profile']['version'] == saved['relationship']['version'] == 1
    prior = {kind:service.vault.decrypt(user_id,'memory',r['id'],service.documents.get(r['blob_id'],user_id))['markdown'] for kind,r in saved.items()}
    assert '本人安排见面' in prior['profile'] and '本人安排见面' in prior['relationship']
    later = 1704153600
    assert upload(client, config, {'kind':'messages','source':'a'*64,'session':'friend','items':[
        {'id':'m:30','database':'message/message_0.db','local_id':30,'timestamp':later,'text':'天气不错','sender':'我','is_me':True}]}).status_code == 200
    with service.db.transaction() as con:
        con.execute(jobs.update().where(jobs.c.user_id == user_id,jobs.c.state == 'queued').values(next_run=0))
    assert Worker(service).run_once()
    with service.db.transaction() as con:
        after = {r['kind']:dict(r) for r in con.execute(select(memories)).mappings()}
    assert after['profile']['version'] == after['relationship']['version'] == 1
    assert after['profile']['blob_id'] == saved['profile']['blob_id']
    assert after['relationship']['blob_id'] == saved['relationship']['blob_id']


def test_historical_addition_preserves_newer_text_and_manual_pins():
    newer = int(datetime(2024, 2, 1,tzinfo=ZONE).timestamp())
    older = int(datetime(2024, 1, 1,tzinfo=ZONE).timestamp())
    previous={'markdown':'# 人物关系\n\n目前同事关系明确。','source_sessions':['s'], 'evidence':[],
              'latest_fact_at':newer,'manual':False}
    added=Worker.apply_memory_updates(previous,[('当时是同学',[{'id':'a','timestamp':older,'is_me':False}])],'s','relationship')
    assert added['markdown'].startswith(previous['markdown'])
    assert '历史补录' not in added['markdown'] and added['latest_fact_at'] == newer
    assert '2024-' not in added['markdown'] and '证据' not in added['markdown']
    protected=Worker.apply_memory_updates(previous,[('现在是同学',[{'id':'a','timestamp':older,'is_me':False}], '目前同事关系明确。')],'s','relationship')
    assert protected is None
    updated=Worker.apply_memory_updates(previous,[('目前是朋友',[{'id':'b','timestamp':newer+100,'is_me':False}], '目前同事关系明确。')],'s','relationship')
    assert '目前是朋友' in updated['markdown'] and '目前同事关系明确。' not in updated['markdown']
    assert Worker.apply_memory_updates({**previous,'manual':True},[('当时是同学',[{'id':'a','timestamp':older}])],'s','relationship') is None


def test_invalid_or_other_person_evidence_does_not_update_memory(platform, monkeypatch):
    client, user_id = user(platform); _, service, _ = platform
    config=device(client); sid,_=seed(client,config)
    client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True,'cycles':['day']})
    monkeypatch.setenv('WECHAT_MODEL_ALLOW_PRIVATE','1')
    service.save_model(user_id,ModelConfig(base_url='http://127.0.0.1:9999/v1',model='fake'))
    def model(config,prompt,purpose,before_call=None):
        if before_call: before_call()
        if '仅输出 JSON' not in purpose: return '摘要',{}
        other = next(e['id'] for e in json.loads(prompt)['可用证据'] if not e['is_me'])
        return json.dumps({'relationship':[],'profile':[{'text':'不允许','evidence':[other]}]}),{}
    monkeypatch.setattr('backend.platform_jobs.call_model',model)
    assert Worker(service).run_once()
    items = client.get('/api/v2/memories').json()['items']
    assert {item['kind'] for item in items} == {'day'}
    job=client.get('/api/v2/manage/jobs').json()['items'][0]
    assert job['state'] == 'done' and '已跳过 1 条' in job['error']


def test_malformed_individual_suggestion_is_skipped_and_many_valid_refs_are_allowed(monkeypatch):
    evidence=[{'id':str(i).zfill(64),'timestamp':i+1,'sender':'我','is_me':True} for i in range(40)]
    worker=Worker(None)
    def evaluate(job,config,prompt,purpose):
        return json.dumps({'relationship':[{'text':'不应覆盖多条记录','replace':'旧条目一\n旧条目二','evidence':['E1']}],
            'profile':[{'text':'有效事实','replace':'','evidence':[f'E{i}' for i in range(1,41)]}]})
    monkeypatch.setattr(worker,'model',evaluate)
    previous={'markdown':''}
    result=worker.memory_updates({'user_id':'x','id':'j'}, {}, '摘要', evidence, previous, previous)
    assert result['_rejected']['relationship']==1 and result['relationship']==[]
    assert result['_rejected']['profile']==0 and len(result['profile'][0][1])==40
