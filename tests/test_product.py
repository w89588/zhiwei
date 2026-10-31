import asyncio
from datetime import datetime
import json
import time
import httpx
import pytest
from test_integration import desktop
from helper import knowledge, memory_store as store, calendar_sync as cal, scheduler, storage


def response():
    return {'summary':'2024-10-04：确认交付边界，并提出下次沟通计划。',
            'facts':[
                {'subject':'other','category':'boundaries','text':'不接受未经确认的交付承诺','certainty':'observed','source_ids':['message/message_0.db:0']},
                {'subject':'self','category':'style','text':'表达偏向简洁，先说明具体行动','certainty':'inference','source_ids':['message/message_0.db:1']},
                {'subject':'self','category':'identity','text':'错误：把对方说的话归给自己','source_ids':['message/message_0.db:0']},
                {'subject':'other','category':'identity','text':'错误：没有聊天证据','source_ids':['made-up']}],
            'events':[{'title':'项目对齐','start':'2024-10-07T15:00:00+08:00','end':'2024-10-07T16:00:00+08:00','all_day':False,'source_ids':['message/message_0.db:1']}]}


def model(monkeypatch):
    calls=[]
    monkeypatch.setattr(knowledge.ai,'config',lambda *a:{'chunk_chars':4000,'context_messages':10})
    async def complete(messages):calls.append(messages);return json.dumps(response(),ensure_ascii=False)
    monkeypatch.setattr(knowledge.ai,'complete',complete)
    return calls


def test_periods_use_beijing_calendar_and_leap_months():
    start,end,label=knowledge.period_bounds('day','2024-02-29')
    assert end-start+1==86400
    assert datetime.fromtimestamp(start,knowledge.TZ).isoformat()=='2024-02-29T00:00:00+08:00'
    start,end,_=knowledge.period_bounds('month','2024-02-29')
    assert end-start+1==29*86400
    start,end,label=knowledge.period_bounds('week','2026-01-01')
    assert label=='2025-12-29 — 2026-01-04' and end-start+1==7*86400
    assert knowledge.previous_anchor('month',datetime(2026,1,1,tzinfo=knowledge.TZ))=='2025-12-31'


def test_learning_evidence_self_scope_idempotence_and_human_corrections(desktop,monkeypatch):
    _,_,_,w,tmp=desktop
    calls=model(monkeypatch);account=store.account_key(w.root);users={'friend'}
    result=asyncio.run(knowledge.learn(w,'friend','day','2024-10-04'))
    assert result['count']==6 and result['facts']==2 and result['events']==1
    person=store.facts(account,users,'friend');own=store.facts(account,users,subject='self')
    assert len(person)==len(own)==1 and person[0]['status']=='pending'
    assert person[0]['evidence'][0]['id']=='message/message_0.db:0'
    assert own[0]['evidence'][0]['is_me']
    assert knowledge.confirmed_context(w,'friend')=={'person':[],'self':[]}
    unchanged=asyncio.run(knowledge.learn(w,'friend','day','2024-10-04'))
    assert unchanged['state']=='unchanged' and len(calls)==1
    store.save_fact(account,users,{'text':'人工修正：交付前先确认范围','category':'boundaries','status':'confirmed'},person[0]['id'])
    # Changed source data triggers a new pass without replacing the user's correction.
    import sqlite3
    with sqlite3.connect(w.cache['message/message_0.db'][1]) as con:
        con.execute('UPDATE [Msg_'+__import__('hashlib').md5(b'friend').hexdigest()+"] SET message_content='新增依据' WHERE local_id=0")
    asyncio.run(knowledge.learn(w,'friend','day','2024-10-04'))
    assert len(calls)==2 and len(store.facts(account,users,'friend'))==1
    corrected=store.facts(account,users,'friend')[0]
    assert corrected['text']=='人工修正：交付前先确认范围' and corrected['status']=='confirmed'
    assert knowledge.confirmed_context(w,'friend')['person'][0]['text']==corrected['text']
    assert store.facts(account,set(),subject='self')==[]
    assert store.facts('different-account',users,'friend')==[]
    assert len(store.journals(account,users,'friend','day'))==1
    assert not list(storage.DATA.glob('analysis-*.txt'))


def test_malformed_model_output_commits_nothing(desktop,monkeypatch):
    _,_,_,w,_=desktop
    model(monkeypatch)
    async def invalid(messages):return '{incomplete'
    monkeypatch.setattr(knowledge.ai,'complete',invalid)
    with pytest.raises(ValueError,match='知识结构'):asyncio.run(knowledge.learn(w,'friend','day','2024-10-04'))
    assert store.journals(store.account_key(w.root),{'friend'})==[]
    assert not list(storage.DATA.glob('analysis-*.txt'))


def test_product_api_scope_manual_knowledge_events_and_mcp_context(desktop):
    client,server,access,w,_=desktop
    ui={'X-Helper-Token':server.TOKEN}
    assert client.get('/api/knowledge',params={'user':'outside'},headers=ui).status_code==403
    assert client.post('/api/knowledge/run',json={'user':'friend','anchor':'invalid'},headers=ui).status_code==400
    fact={'user':'friend','category':'boundaries','text':'需要明确交付时间'}
    created=client.post('/api/knowledge',json=fact,headers=ui)
    assert created.status_code==200
    mine={'subject':'self','category':'style','text':'喜欢直接说明安排'}
    assert client.post('/api/knowledge',json=mine,headers=ui).status_code==200
    assert client.get('/api/product',headers=ui).json()['facts']==1
    access.configure(True)
    context=client.get('/api/external/context',params={'user':'friend'},headers={'X-Wechat-Integration-Token':access.token}).json()
    assert context['knowledge']['person'][0]['text']==fact['text']
    assert context['knowledge']['self'][0]['text']==mine['text']
    value={'user':'friend','title':'确认需求','start':'2026-10-15','all_day':True}
    event=client.post('/api/calendar/events',json=value,headers=ui)
    assert event.status_code==200
    assert 'DTEND;VALUE=DATE:20261016' in client.get('/api/calendar/export',headers=ui).text
    w.update_workspace('friend',False)
    assert client.get('/api/knowledge',params={'user':'friend'},headers=ui).status_code==403
    assert client.get('/api/calendar/events',headers=ui).json()['items']==[]
    assert client.post('/api/knowledge/facts/'+created.json()['id'],json=fact,headers=ui).status_code==400


def test_calendar_ics_utf8_folding_dates_recurrence_and_confirmation(desktop):
    _,_,_,w,_=desktop
    account=store.account_key(w.root);users={'friend'}
    key=cal.save_event(account,users,{'user':'friend','title':'纪念日,'+'很长的中文标题'*20,'start':'2024-02-29','all_day':True,'recurrence':'yearly','description':'第一行\nSUMMARY:不会成为新属性'})
    event=cal.event_by_id(account,users,key)
    text=cal.icalendar([event])
    assert 'DTSTART;VALUE=DATE:20240229' in text and 'DTEND;VALUE=DATE:20240301' in text
    assert 'RRULE:FREQ=YEARLY' in text and '\\nSUMMARY:' in text
    assert all(len(line.encode('utf-8'))<=75 for line in text.split('\r\n'))
    assert cal.events(account,users,'2026-02')==[]
    assert cal.events(account,users,'2028-02')[0]['display_start']=='2028-02-29'
    draft=cal.save_event(account,users,{'title':'日期待确认','status':'draft','start':''})
    assert cal.event_by_id(account,users,draft)['status']=='draft'
    with pytest.raises(ValueError,match='确认日程日期'):cal.save_event(account,users,{'status':'confirmed'},draft)


def test_caldav_discovery_password_encryption_etags_and_conflicts(desktop,monkeypatch):
    _,_,_,w,tmp=desktop
    account=store.account_key(w.root);users={'friend'}
    cfg={'url':'https://calendar.example/dav/','username':'tester','password':'test-secret-value','calendar_url':'https://calendar.example/dav/cal/'}
    assert cal.save_settings(account,cfg)['has_password']
    assert b'test-secret-value' not in (tmp/'caldav.bin').read_bytes()
    requests=[];conflict=[False]
    def handler(request):
        requests.append(request)
        if request.method=='PROPFIND':
            return httpx.Response(207,text='<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/dav/cal/</d:href><d:propstat><d:prop><d:displayname>个人日历</d:displayname><d:resourcetype><c:calendar/></d:resourcetype></d:prop></d:propstat></d:response></d:multistatus>')
        if request.method=='PUT':return httpx.Response(412 if conflict[0] else 201,headers={'ETag':'"v1"'})
        return httpx.Response(200)
    monkeypatch.setattr(cal,'client',lambda cfg:httpx.AsyncClient(transport=httpx.MockTransport(handler),auth=(cfg['username'],cfg['password'])))
    assert asyncio.run(cal.discover(account))[0]['name']=='个人日历'
    key=cal.save_event(account,users,{'user':'friend','title':'对齐','start':'2026-10-15'})
    cal.save_event(account,users,{'user':'friend','title':'未确认','start':'2026-10-16','status':'draft'})
    first=asyncio.run(cal.sync(account,users));assert first=={'synced':1,'conflicts':0,'errors':0}
    put=[r for r in requests if r.method=='PUT'][0]
    assert put.headers['If-None-Match']=='*' and 'DTSTART;VALUE=DATE:20261015' in put.content.decode()
    assert asyncio.run(cal.sync(account,users))['synced']==0
    cal.save_event(account,users,{'title':'修改后的安排'},key);conflict[0]=True
    assert asyncio.run(cal.sync(account,users))['conflicts']==1
    assert [r for r in requests if r.method=='PUT'][-1].headers['If-Match']=='"v1"'
    assert cal.event_by_id(account,users,key)['sync_state']=='conflict'
    with pytest.raises(ValueError,match='不同域名'):cal.safe_href(cfg['url'],'https://other.example/cal/')
    with pytest.raises(ValueError,match='重新填写'):cal.save_settings(account,cfg|{'url':'https://other.example/dav/','password':''})


def test_scheduler_disabled_defaults_period_once_and_lease(desktop,monkeypatch):
    _,_,_,w,_=desktop
    calls=model(monkeypatch);account=store.account_key(w.root)
    now=datetime(2024,10,5,9,tzinfo=knowledge.TZ)
    assert asyncio.run(scheduler.tick(w,now))['processed']==0 and not calls
    scheduler.save_configuration(account,{'friend'},{'day':True,'time':'08:00'})
    assert asyncio.run(scheduler.tick(w,now))['processed']==1 and len(calls)==1
    assert asyncio.run(scheduler.tick(w,now))['processed']==0 and len(calls)==1
    assert store.acquire_lease(account,'other-worker')
    assert asyncio.run(scheduler.tick(w,now,True))['state']=='busy'
    store.release_lease(account,'other-worker')
    assert asyncio.run(scheduler.tick(w,now,True))['processed']==1 and len(calls)==1
    assert store.recent_runs(account,{'friend'})[0]['state']=='unchanged'


def test_async_learning_job_and_evidence_api(desktop,monkeypatch):
    client,server,_,w,_=desktop
    model(monkeypatch);ui={'X-Helper-Token':server.TOKEN}
    with client:
        started=client.post('/api/knowledge/run',json={'user':'friend','period':'day','anchor':'2024-10-04'},headers=ui)
        assert started.status_code==200,started.text
        key=started.json()['id']
        for _ in range(200):
            job=client.get('/api/product/jobs/'+key,headers=ui).json()
            if job['state']!='running':break
            time.sleep(.01)
        assert job['state']=='done',job
        assert client.get('/api/knowledge',params={'user':'friend'},headers=ui).json()['items'][0]['status']=='pending'
        assert client.get('/api/journals',params={'user':'friend','period':'day'},headers=ui).json()['items'][0]['count']==6


def test_cancel_and_revoked_scope_never_commit(desktop,monkeypatch):
    _,_,_,w,_=desktop
    model(monkeypatch)
    async def scenario():
        entered=asyncio.Event()
        async def waiting(messages):
            entered.set();await asyncio.sleep(100)
        monkeypatch.setattr(knowledge.ai,'complete',waiting)
        task=asyncio.create_task(knowledge.learn(w,'friend','day','2024-10-04'))
        await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert store.journals(store.account_key(w.root),{'friend'})==[]
        async def removing(messages):
            w.update_workspace('friend',False)
            return json.dumps(response())
        monkeypatch.setattr(knowledge.ai,'complete',removing)
        with pytest.raises(ValueError,match='移出'):await knowledge.learn(w,'friend','day','2024-10-04')
        assert store.journals(store.account_key(w.root),{'friend'})==[]
    asyncio.run(scenario())
    assert not list(storage.DATA.glob('analysis-*.txt'))


def test_scheduler_error_retries_without_marking_complete(desktop,monkeypatch):
    _,_,_,w,_=desktop
    account=store.account_key(w.root);model(monkeypatch)
    scheduler.save_configuration(account,{'friend'},{'day':True})
    calls=[]
    async def failing(messages):calls.append(messages);raise ValueError('测试模型失败')
    monkeypatch.setattr(knowledge.ai,'complete',failing)
    now=datetime(2024,10,5,9,tzinfo=knowledge.TZ)
    assert asyncio.run(scheduler.tick(w,now))['errors']==1
    assert store.get_setting(account,'schedule_completed',{})=={}
    assert asyncio.run(scheduler.tick(w,now))['processed']==0 and len(calls)==1
    assert asyncio.run(scheduler.tick(w,now,force=True))['errors']==1 and len(calls)==2
    assert store.acquire_lease(account,'next-worker')
    store.release_lease(account,'next-worker')


def test_legacy_context_migrates_to_one_account_only(desktop):
    client,server,_,w,_=desktop
    ui={'X-Helper-Token':server.TOKEN}
    storage.write_json('profiles.json',{'friend':{'goals':'原账号沟通目标'}})
    storage.write_json('summaries.json',{'friend':[{'text':'原账号总结'}]})
    root=w.root
    w.current={'path':str(root)}
    assert client.get('/api/profile',params={'user':'friend'},headers=ui).json()['goals']=='原账号沟通目标'
    assert client.get('/api/history',params={'user':'friend'},headers=ui).json()[0]['text']=='原账号总结'
    w.root=root.parent.parent/'second-account'/'db_storage';w.update_workspace('friend',True)
    assert client.get('/api/profile',params={'user':'friend'},headers=ui).json()['goals']==''
    assert client.get('/api/history',params={'user':'friend'},headers=ui).json()==[]
    w.root=root
    assert client.get('/api/history',params={'user':'friend'},headers=ui).json()[0]['text']=='原账号总结'


def test_caldav_concurrent_edit_and_removed_sources(desktop,monkeypatch):
    _,_,_,w,_=desktop
    account=store.account_key(w.root);users={'friend'}
    cal.save_settings(account,{'url':'https://calendar.example/dav/','username':'test','password':'synthetic','calendar_url':'https://calendar.example/cal/'})
    key=cal.save_event(account,users,{'user':'friend','title':'原安排','start':'2026-10-15'})
    requests=[]
    def handler(request):
        requests.append(request)
        cal.save_event(account,users,{'title':'同步过程中人工修改'},key)
        return httpx.Response(201,headers={'ETag':'"first"'})
    monkeypatch.setattr(cal,'client',lambda cfg:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(cal.sync(account,users))
    event=cal.event_by_id(account,users,key)
    assert event['title']=='同步过程中人工修改' and event['sync_state']=='pending' and event['remote_etag']=='"first"'
    assert asyncio.run(cal.sync(account,users,active_users=lambda:set()))['synced']==0
    assert len(requests)==1


def test_windows_background_task_mocked_and_paths_quoted(desktop,monkeypatch):
    from helper import windows_tasks
    import subprocess
    from pathlib import Path
    calls=[]
    def fake_run(args,**kwargs):
        assert kwargs['creationflags']==subprocess.CREATE_NO_WINDOW
        calls.append(args);return subprocess.CompletedProcess(args,0)
    monkeypatch.setattr(windows_tasks.subprocess,'run',fake_run)
    monkeypatch.setattr(windows_tasks.sys,'frozen',True,raising=False)
    executable=storage.DATA/'with spaces'/'WechatHelper.exe';executable.parent.mkdir();(executable.parent/'WechatHelperTasks.exe').touch()
    monkeypatch.setattr(windows_tasks.sys,'executable',str(executable))
    assert '"'+str(executable.parent/'WechatHelperTasks.exe')+'"' in windows_tasks.command()
    assert windows_tasks.configure(True)['enabled']
    assert '/IT' in calls[0] and calls[0][calls[0].index('/MO')+1]=='15'
    assert not windows_tasks.configure(False)['enabled']
    assert calls[-1][1]=='/Delete'


def test_background_registration_moves_enabled_task_once(desktop,monkeypatch):
    from helper import windows_tasks
    import subprocess
    calls=[]
    monkeypatch.setattr(windows_tasks.sys,'frozen',True,raising=False)
    executable=storage.DATA/'new install'/'WechatHelper.exe'
    executable.parent.mkdir();(executable.parent/'WechatHelperTasks.exe').touch()
    monkeypatch.setattr(windows_tasks.sys,'executable',str(executable))
    monkeypatch.setattr(windows_tasks.subprocess,'run',lambda args,**kw:(calls.append(args) or subprocess.CompletedProcess(args,0)))
    assert not windows_tasks.refresh_registration() and not calls
    storage.write_json('background.json',{'enabled':True,'command':'old-install.exe'})
    assert windows_tasks.refresh_registration()
    assert str(executable.parent/'WechatHelperTasks.exe') in calls[0][calls[0].index('/TR')+1]
    assert not windows_tasks.refresh_registration() and len(calls)==1
    storage.write_json('background.json',{'enabled':False,'command':'old-install.exe'})
    assert not windows_tasks.refresh_registration() and len(calls)==1


def test_background_registration_failure_preserves_settings(desktop,monkeypatch):
    from helper import windows_tasks
    import subprocess
    monkeypatch.setattr(windows_tasks.sys,'frozen',True,raising=False)
    executable=storage.DATA/'WechatHelper.exe';(storage.DATA/'WechatHelperTasks.exe').touch()
    monkeypatch.setattr(windows_tasks.sys,'executable',str(executable))
    monkeypatch.setattr(windows_tasks.subprocess,'run',lambda args,**kw:subprocess.CompletedProcess(args,1))
    previous={'enabled':True,'command':'old-install.exe'}
    storage.write_json('background.json',previous)
    assert not windows_tasks.refresh_registration()
    assert storage.read_json('background.json',{})==previous
    assert (storage.DATA/'background-registration-error.log').exists()


def test_source_background_task_uses_windowless_python(desktop,monkeypatch):
    from helper import windows_tasks
    monkeypatch.setattr(windows_tasks.sys,'frozen',False,raising=False)
    executable=storage.DATA/'python.exe'
    monkeypatch.setattr(windows_tasks.sys,'executable',str(executable))
    with pytest.raises(ValueError,match='pythonw.exe'):windows_tasks.command()
    executable.with_name('pythonw.exe').touch()
    assert 'pythonw.exe' in windows_tasks.command()


def test_reply_uses_both_confirmed_memories_and_journals(desktop,monkeypatch):
    _,server,_,w,_=desktop
    account=store.account_key(w.root);users={'friend'}
    store.save_fact(account,users,{'user':'friend','category':'boundaries','text':'不在周末谈工作'})
    store.save_fact(account,users,{'subject':'self','category':'style','text':'先说明行动再解释'})
    store.save_fact(account,users,{'user':'friend','text':'未经确认的性格猜测','status':'pending'})
    store.commit_learning(account,'friend','day',1,2,'测试日记','历史计划需要再次确认',{'count':1,'digest':'test'},[],[])
    seen=[]
    async def complete(messages):
        raw=messages[-1]['content'];payload=json.loads(raw[raw.index('{"profile"'):]);seen.append(payload)
        return json.dumps({'analysis':'依据双方边界与风格','risks':[],'replies':[{'text':'候选'+str(i)} for i in range(3)],'follow_up':''})
    monkeypatch.setattr(server.ai,'complete',complete)
    async def scenario():
        key=(await server.run_ai(server.Analysis(user='friend',action='advice',scenario='确认会议')))['id']
        for _ in range(100):
            if server.jobs[key]['state']!='running':break
            await asyncio.sleep(.01)
        assert server.jobs[key]['state']=='done',server.jobs[key]
    asyncio.run(scenario())
    memory=seen[0]['profile']['knowledge']
    assert memory['person'][0]['text']=='不在周末谈工作'
    assert memory['self'][0]['text']=='先说明行动再解释'
    assert '未经确认的性格猜测' not in json.dumps(seen,ensure_ascii=False)
    assert seen[0]['profile']['period_summaries'][0]['text']=='历史计划需要再次确认'


def test_headless_worker_skips_unconfigured_account(tmp_path):
    import subprocess,sys
    from pathlib import Path
    result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'tasks_main.py'),'--once','--data',str(tmp_path)],capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr.decode(errors='replace')
    assert not (tmp_path/'background-error.log').exists()


def test_collector_desktop_api_has_no_local_export():
    from main import DesktopAPI
    assert not hasattr(DesktopAPI, 'save_export')


def test_calendar_recurring_cross_year_and_exclusive_midnight(desktop):
    _,_,_,w,_=desktop
    account=store.account_key(w.root);users={'friend'}
    cal.save_event(account,users,{'title':'跨年纪念','start':'2024-12-31','end':'2025-01-02','recurrence':'yearly'})
    assert cal.events(account,users,'2026-01')[0]['display_start']=='2025-12-31'
    assert cal.events(account,users,'2023-12')==[]
    cal.save_event(account,users,{'title':'跨夜安排','start':'2026-09-30T23:00:00+08:00','end':'2026-10-01T00:00:00+08:00','all_day':False})
    assert not any(e['title']=='跨夜安排' for e in cal.events(account,users,'2026-10'))
