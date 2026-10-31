"""Persistent periodic learning; a lease prevents desktop/background duplicate work."""
import asyncio
from datetime import datetime
import time
import uuid
from . import knowledge, memory_store as store, calendar_sync

DEFAULT={'day':False,'week':False,'month':False,'time':'08:00','users':[]}


def configuration(account):
    return DEFAULT|store.get_setting(account,'schedule',{})


def save_configuration(account,users,value):
    config=DEFAULT|value
    try:datetime.strptime(config['time'],'%H:%M')
    except (ValueError,TypeError) as error:raise ValueError('任务时间应为 HH:MM') from error
    if any(user not in users for user in config['users']):raise ValueError('任务只能使用工作台中的会话')
    store.set_setting(account,'schedule',config)
    return config


async def tick(wechat,now=None,force=False,progress=lambda message:None):
    if wechat.status['state']!='ready' or not wechat.root:return {'state':'waiting','message':'等待微信连接'}
    root=wechat.root;account=store.account_key(root);users=set(wechat.workspace());config=configuration(account)
    now=now or datetime.now(knowledge.TZ)
    if not force and now.strftime('%H:%M')<config['time']:return {'state':'waiting','message':'等待每日执行时间'}
    if not users and not calendar_sync.settings(account,False)['auto_sync']:return {'state':'empty','message':'工作台没有会话'}
    owner=uuid.uuid4().hex
    if not store.acquire_lease(account,owner):return {'state':'busy','message':'另一个整理任务正在执行'}
    def guard():
        if wechat.root!=root or wechat.status['state']!='ready':raise ValueError('微信账号已切换，自动化已停止')
        if not store.acquire_lease(account,owner):raise ValueError('整理任务锁已失效')
    async def heartbeat():
        while True:
            await asyncio.sleep(40)
            if not store.acquire_lease(account,owner):return
    renewal=asyncio.create_task(heartbeat())
    completed=store.get_setting(account,'schedule_completed',{})
    failures=store.get_setting(account,'schedule_failures',{})
    report={'state':'done','processed':0,'errors':0}
    try:
        for period in ('day','week','month'):
            if not config[period]:continue
            anchor=knowledge.previous_anchor(period,now)
            targets=config['users'] or sorted(users)
            for user in targets:
                guard()
                if not wechat.in_workspace(user):continue
                key=user+'|'+period;marker=anchor
                if not force and completed.get(key)==marker:continue
                failed=failures.get(key,{})
                if not force and failed.get('retry_at',0)>time.time():continue
                run=store.run_started(account,user,period,anchor)
                try:
                    progress(f'正在执行 {period} 周期整理')
                    result=await knowledge.learn(wechat,user,period,anchor,progress,guard)
                    store.finish_run(run,result['state'],result['message'])
                    completed[key]=marker;failures.pop(key,None);report['processed']+=1
                except asyncio.CancelledError:
                    store.finish_run(run,'cancelled','整理已取消');raise
                except Exception as error:
                    message=str(error)[:500]
                    store.finish_run(run,'error',message);report['errors']+=1
                    attempts=int(failed.get('attempts',0))+1
                    failures[key]={'attempts':attempts,'retry_at':time.time()+min(3600,300*2**min(attempts-1,4))}
                store.set_setting(account,'schedule_completed',completed)
                store.set_setting(account,'schedule_failures',failures)
        guard()
        cfg=calendar_sync.settings(account,False)
        pending=any(e['status']=='confirmed' and e['sync_state'] not in {'synced','conflict'} for e in calendar_sync.events(account,set(wechat.workspace())))
        retry_at=store.get_setting(account,'calendar_retry_at',0)
        if cfg['auto_sync'] and cfg['calendar_url'] and pending and (force or time.time()>=retry_at):
            run=store.run_started(account,'','calendar','CalDAV 自动同步')
            try:
                result=await calendar_sync.sync(account,set(wechat.workspace()),guard,lambda:set(wechat.workspace()),retry_conflicts=False)
                store.finish_run(run,'done' if not result['errors'] and not result['conflicts'] else 'error',f"同步 {result['synced']} 项，冲突 {result['conflicts']} 项，错误 {result['errors']} 项")
                report['errors']+=result['errors']+result['conflicts']
                store.set_setting(account,'calendar_retry_at',time.time()+300 if result['errors'] else 0)
            except asyncio.CancelledError:
                store.finish_run(run,'cancelled','同步已取消');raise
            except Exception as error:
                store.finish_run(run,'error',str(error)[:500]);report['errors']+=1
                store.set_setting(account,'calendar_retry_at',time.time()+300)
        return report
    finally:
        renewal.cancel()
        await asyncio.gather(renewal,return_exceptions=True)
        store.release_lease(account,owner)


async def loop(provider):
    while True:
        try:await tick(provider())
        except asyncio.CancelledError:raise
        except Exception:
            # A temporary connection/storage error is retried on the next pass.
            pass
        await asyncio.sleep(60)
