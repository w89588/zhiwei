"""Product 1.0 API, isolated from database-reader and read-only MCP transport."""
import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from . import knowledge, memory_store as store, calendar_sync, scheduler

jobs={};tasks={}


class FactValue(BaseModel):
    user:str=''
    subject:Literal['self','other']='other'
    category:Literal['identity','personality','preferences','style','relationship','boundaries','goals']='identity'
    text:str=Field(min_length=1,max_length=2000)
    status:Literal['pending','confirmed','rejected']='confirmed'


class Learning(BaseModel):
    user:str=Field(min_length=1,max_length=256)
    period:Literal['day','week','month','all']='day'
    anchor:str|None=None


class EventValue(BaseModel):
    user:str=''
    title:str=Field(min_length=1,max_length=200)
    start:str=''
    end:str=''
    all_day:bool=True
    recurrence:Literal['none','yearly']='none'
    description:str=Field(default='',max_length=4000)
    location:str=Field(default='',max_length=500)
    status:Literal['draft','confirmed','ignored']='confirmed'


class CalendarSettings(BaseModel):
    url:str=Field(max_length=2048)
    username:str=Field(max_length=500)
    password:str=Field(default='',max_length=2000)
    calendar_url:str=Field(default='',max_length=2048)
    auto_sync:bool=False


class ScheduleSettings(BaseModel):
    day:bool=False
    week:bool=False
    month:bool=False
    time:str='08:00'
    users:list[str]=Field(default_factory=list,max_length=1000)


class BackgroundSettings(BaseModel):
    enabled:bool


def cancel(user=None):
    for key,task in list(tasks.items()):
        if user is None or jobs.get(key,{}).get('user')==user:
            task.get_loop().call_soon_threadsafe(task.cancel)


def install(app,provider):
    router=APIRouter(prefix='/api')
    def scope(user=None,ready=False):
        w=provider()
        with w.lock:
            if not w.root:raise HTTPException(409,'请先连接微信账号')
            if ready and w.status['state']!='ready':raise HTTPException(409,'请等待账号连接完成')
            users=set(w.workspace())
            if user and user not in users:raise HTTPException(403,'该会话未加入工作台')
            return w,store.account_key(w.root),users
    def value_error(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except (ValueError,TypeError) as error:raise HTTPException(400,str(error)) from error

    async def exclusive(account,guard,work):
        owner=uuid.uuid4().hex
        if not store.acquire_lease(account,owner):raise ValueError('另一个整理或同步任务正在执行，请稍后重试')
        async def heartbeat():
            while True:
                await asyncio.sleep(40)
                if not store.acquire_lease(account,owner):return
        renewal=asyncio.create_task(heartbeat())
        def check():
            guard()
            if not store.acquire_lease(account,owner):raise ValueError('任务锁已失效，请重试')
        try:return await work(check)
        finally:
            renewal.cancel();await asyncio.gather(renewal,return_exceptions=True)
            store.release_lease(account,owner)

    def launch(w,account,user,work,label,period=''):
        key=uuid.uuid4().hex
        root=w.root
        jobs[key]={'id':key,'account':account,'user':user,'state':'running','message':label,'result':None}
        def guard():
            if w.root!=root:raise ValueError('账号已切换，任务已停止')
            if user and not w.in_workspace(user):raise ValueError('来源会话已移出工作台，任务已停止')
        def progress(message):
            guard();jobs[key]['message']=message
        async def run():
            record=store.run_started(account,user,period,label)
            try:
                guard();result=await work(progress,guard)
                guard();message=result.get('message','任务已完成')
                jobs[key].update(state='done',message=message,result=result)
                store.finish_run(record,'done',message)
            except asyncio.CancelledError:
                jobs[key].update(state='cancelled',message='任务已取消');store.finish_run(record,'cancelled','任务已取消')
            except Exception as error:
                message=str(error)[:500]
                jobs[key].update(state='error',message=message);store.finish_run(record,'error',message)
            finally:tasks.pop(key,None)
        tasks[key]=asyncio.create_task(run())
        for old in list(jobs):
            if len(jobs)<=100:break
            if jobs[old]['state']!='running':jobs.pop(old,None)
        return {'id':key}

    @router.get('/product')
    def product():
        w,account,users=scope()
        facts=store.facts(account,users,subject='other');own=store.facts(account,users,subject='self')
        events=calendar_sync.events(account,users)
        return {'version':'1.0.1','people':len(users),'facts':len(facts),'self_facts':len(own),
                'pending_facts':sum(f['status']=='pending' for f in facts+own),
                'pending_events':sum(e['status']=='draft' for e in events),'events':sum(e['status']=='confirmed' for e in events)}

    @router.get('/knowledge')
    def read_knowledge(user:str|None=None,subject:Literal['self','other']='other',include_rejected:bool=False):
        w,account,users=scope(user if subject=='other' else None)
        return {'items':store.facts(account,users,user,subject,include_rejected),'subject':subject,'user':user}

    @router.post('/knowledge')
    def create_fact(body:FactValue):
        w,account,users=scope(body.user if body.subject=='other' else None)
        return {'id':value_error(store.save_fact,account,users,body.model_dump())}

    @router.post('/knowledge/facts/{fact_id}')
    def update_fact(fact_id:str,body:FactValue):
        w,account,users=scope()
        return {'id':value_error(store.save_fact,account,users,body.model_dump(),fact_id)}

    @router.get('/journals')
    def journals(user:str|None=None,period:Literal['day','week','month','all']|None=None,limit:int=Query(100,ge=1,le=300)):
        w,account,users=scope(user)
        return {'items':store.journals(account,users,user,period,limit)}

    @router.post('/knowledge/run')
    async def run_learning(body:Learning):
        w,account,users=scope(body.user,True)
        value_error(knowledge.period_bounds,body.period,body.anchor)
        if any(j['account']==account and j['user']==body.user and j['state']=='running' for j in jobs.values()):raise HTTPException(409,'该会话已有整理任务正在执行')
        async def work(progress,guard):
            return await exclusive(account,guard,lambda check:knowledge.learn(w,body.user,body.period,body.anchor,progress,check))
        return launch(w,account,body.user,work,'整理联系人知识库',body.period)

    @router.get('/product/jobs/{key}')
    def get_job(key:str):
        w,account,users=scope()
        job=jobs.get(key)
        if not job or job['account']!=account:raise HTTPException(404,'任务不存在')
        if job['user'] and job['user'] not in users:raise HTTPException(403,'该会话已移出工作台')
        return {k:v for k,v in job.items() if k!='account'}

    @router.post('/product/jobs/{key}/cancel')
    async def cancel_job(key:str):
        get_job(key)
        task=tasks.get(key)
        if task:task.cancel()
        return {'ok':True}

    @router.get('/calendar/events')
    def read_events(month:str|None=None):
        w,account,users=scope()
        return {'items':value_error(calendar_sync.events,account,users,month)}

    @router.post('/calendar/events')
    def create_event(body:EventValue):
        w,account,users=scope(body.user or None)
        return {'id':value_error(calendar_sync.save_event,account,users,body.model_dump())}

    @router.post('/calendar/events/{key}')
    def update_event(key:str,body:EventValue):
        w,account,users=scope()
        return {'id':value_error(calendar_sync.save_event,account,users,body.model_dump(),key)}

    @router.get('/calendar/export')
    def export_calendar():
        w,account,users=scope()
        data=calendar_sync.icalendar(calendar_sync.events(account,users))
        return Response(data,media_type='text/calendar',headers={'Content-Disposition':'attachment; filename="conversation-calendar.ics"'})

    @router.get('/calendar/settings')
    def calendar_settings():
        w,account,users=scope()
        return calendar_sync.settings(account)

    @router.post('/calendar/settings')
    def configure_calendar(body:CalendarSettings):
        w,account,users=scope()
        return value_error(calendar_sync.save_settings,account,body.model_dump())

    @router.post('/calendar/discover')
    async def discover_calendar():
        w,account,users=scope()
        try:return {'items':await calendar_sync.discover(account)}
        except ValueError as error:raise HTTPException(400,str(error)) from error
        except Exception as error:raise HTTPException(502,'无法连接 CalDAV 服务，请检查地址和网络') from error

    @router.post('/calendar/sync')
    async def sync_calendar():
        w,account,users=scope()
        async def work(progress,guard):
            progress('正在同步已确认日程')
            result=await exclusive(account,guard,lambda check:calendar_sync.sync(account,users,check,lambda:set(w.workspace())))
            return result|{'message':f"同步 {result['synced']} 项，冲突 {result['conflicts']} 项，错误 {result['errors']} 项"}
        return launch(w,account,'',work,'同步 CalDAV 日历','calendar')

    @router.get('/automation')
    def automation():
        from . import windows_tasks
        w,account,users=scope()
        return {'settings':scheduler.configuration(account),'runs':store.recent_runs(account,users),'background':windows_tasks.status()}

    @router.post('/automation')
    def save_automation(body:ScheduleSettings):
        w,account,users=scope()
        return value_error(scheduler.save_configuration,account,users,body.model_dump())

    @router.post('/automation/run')
    async def run_automation():
        w,account,users=scope(ready=True)
        async def work(progress,guard):
            guard();result=await scheduler.tick(w,force=True,progress=progress)
            if result['state']=='busy':raise ValueError(result['message'])
            return result|{'message':result.get('message') or f"已执行 {result.get('processed',0)} 个整理任务，错误 {result.get('errors',0)} 个"}
        return launch(w,account,'',work,'执行周期整理','automation')

    @router.post('/automation/background')
    async def background(body:BackgroundSettings):
        from . import windows_tasks
        scope()
        return value_error(windows_tasks.configure,body.enabled)

    @asynccontextmanager
    async def lifespan(application):
        application.state.memory_scheduler=asyncio.create_task(scheduler.loop(provider))
        try:yield
        finally:
            task=application.state.memory_scheduler
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            pending=list(tasks.values())
            for task in pending:task.cancel()
            if pending:await asyncio.gather(*pending,return_exceptions=True)
    app.router.lifespan_context=lifespan

    app.include_router(router)
