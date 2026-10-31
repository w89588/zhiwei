import asyncio
import json
import secrets
import threading
import time
import uuid
from pathlib import Path
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import FileResponse,StreamingResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from fastapi import Query
from typing import Literal
from .wechat import wechat,discover
from .storage import read_json,write_json
from . import ai
from . import exporting
from .integration import integration,SKILL
from . import knowledge,memory_store,product_api,chat_memory
from .remote_sync import uploader
from .platform_sync import platform_uploader

app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
TOKEN=secrets.token_urlsafe(32)
STATIC=Path(__file__).parent/'static'
jobs={}
tasks={}
stop=threading.Event()

@app.middleware('http')
async def local_only(request:Request,call_next):
    host=request.headers.get('host','').split(':')[0]
    if host not in ('127.0.0.1','localhost','testserver'):
        return Response(status_code=403)
    origin=request.headers.get('origin')
    if origin and origin!=str(request.base_url).rstrip('/'):
        return Response(status_code=403)
    if request.url.path.startswith('/api/external/'):
        if not integration.enabled:
            return Response('外部 AI 接入尚未启用',status_code=403)
        if not secrets.compare_digest(request.headers.get('x-wechat-integration-token',''),integration.token):
            return Response(status_code=401)
    elif request.url.path.startswith('/api/') and request.headers.get('x-helper-token')!=TOKEN:
        return Response(status_code=401)
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    return response

@app.get('/')
def index():
    from fastapi.responses import HTMLResponse
    return HTMLResponse((STATIC/'index.html').read_text('utf-8').replace('__TOKEN__',TOKEN))

app.mount('/static',StaticFiles(directory=STATIC),name='static')

@app.get('/api/status')
def status():
    return dict(wechat.status,accounts=discover(),path=str(wechat.root or ''),databases=len(wechat.cache),workspace_count=len(wechat.workspace()))

class Connect(BaseModel):
    path:str
    scan:bool=False

@app.post('/api/connect')
def connect(body:Connect):
    if wechat.status['state']=='connecting': raise HTTPException(409,'正在连接')
    wechat.status['state']='connecting'
    cancel_workspace_jobs()
    threading.Thread(target=wechat.connect,args=(body.path,body.scan),daemon=True).start()
    return {'ok':True}

@app.get('/api/sessions')
def sessions(q:str=''):
    return wechat.workspace_sessions(q)

@app.get('/api/session-catalog')
def session_catalog(q:str='',offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=200)):
    external_ready()
    with wechat.lock:
        entries=wechat.workspace()
        items=[dict(s,selected=s['id'] in entries,note=entries.get(s['id'],{}).get('note','')) for s in wechat.sessions()]
        items=[s for s in items if q.casefold() in (s['name']+s['id']+s['note']).casefold()]
        return {'items':items[offset:offset+limit],'total':len(items),'has_more':offset+limit<len(items)}

class WorkspaceSelection(BaseModel):
    user:str=Field(min_length=1,max_length=256)
    selected:bool
    note:str|None=Field(default=None,max_length=2000)

@app.post('/api/workspace/session')
def workspace_session(body:WorkspaceSelection):
    try:
        with wechat.lock:
            wechat.update_workspace(body.user,body.selected,body.note)
            if not body.selected:cancel_workspace_jobs(body.user)
            if body.selected:wechat.sync()
            uploader.changed.set()
            return {'ok':True,'generation':wechat.status['generation']}
    except ValueError as error:raise HTTPException(400,str(error))

class SessionNote(BaseModel):
    user:str=Field(min_length=1,max_length=256)
    note:str=Field(max_length=2000)

@app.post('/api/workspace/note')
def workspace_note(body:SessionNote):
    with wechat.lock:
        workspace_ready(body.user)
        wechat.update_workspace(body.user,True,body.note)
        uploader.changed.set()
    return {'ok':True}


class RemoteSettings(BaseModel):
    url:str=Field(default='',max_length=2048)
    token:str=Field(default='',max_length=4096)
    enabled:bool=False


class RemoteRun(BaseModel):
    full:bool=False


@app.get('/api/remote/settings')
def remote_settings():
    return uploader.settings(wechat)


@app.post('/api/remote/settings')
def configure_remote(body:RemoteSettings):
    try:return uploader.configure(wechat,body.url,body.token,body.enabled)
    except ValueError as error:raise HTTPException(400,str(error))


@app.post('/api/remote/test')
def test_remote():
    try:return uploader.test(wechat)
    except ValueError as error:raise HTTPException(400,str(error))


@app.post('/api/remote/sync')
def sync_remote(body:RemoteRun):
    if not uploader.config(wechat).get('enabled'):raise HTTPException(400,'请先开启服务端上传')
    threading.Thread(target=uploader.sync,args=(wechat,body.full,True),daemon=True).start()
    return {'ok':True}


class PlatformSettings(BaseModel):
    url:str=Field(default='',max_length=2048)
    app_key:str=Field(default='',max_length=64)
    app_secret:str=Field(default='',max_length=128)
    enabled:bool=False
    scope:Literal['selected','all']='selected'


@app.get('/api/platform/settings')
def platform_settings():
    return platform_uploader.settings(wechat)


@app.post('/api/platform/settings')
def configure_platform(body:PlatformSettings):
    try:return platform_uploader.configure(wechat,body.url,body.app_key,body.app_secret,body.enabled,body.scope)
    except ValueError as error:raise HTTPException(400,str(error))


@app.post('/api/platform/test')
def test_platform():
    try:return platform_uploader.test(wechat)
    except ValueError as error:raise HTTPException(400,str(error))


@app.post('/api/platform/sync')
def sync_platform(body:RemoteRun):
    if not platform_uploader.config(wechat).get('enabled'):raise HTTPException(400,'请先开启平台上传')
    threading.Thread(target=platform_uploader.sync,args=(wechat,body.full),daemon=True).start()
    return {'ok':True}

def workspace_ready(user):
    external_ready()
    if not wechat.in_workspace(user):raise HTTPException(403,'该会话未加入对话工作台，请先在工作台添加会话')

def cancel_workspace_jobs(user=None):
    product_api.cancel(user)
    for key,task in list(tasks.items()):
        if user is None or jobs.get(key,{}).get('user')==user:
            task.get_loop().call_soon_threadsafe(task.cancel)

@app.get('/api/messages')
def messages(user:str,offset:int=0,limit:int=100,q:str='',start:int=0,end:int=0):
    with wechat.lock:
        workspace_ready(user)
        return wechat.messages(user,max(0,offset),max(1,min(1000,limit)),q,start,end)

@app.get('/api/export')
def export(user:str,format:Literal['json','markdown','txt']='json',q:str='',start:int=Query(0,ge=0),end:int=Query(0,ge=0)):
    if end and end<start:raise HTTPException(422,'结束时间不能早于开始时间')
    with wechat.lock:
        workspace_ready(user)
        items=wechat.messages(user,0,2**31-1,q,start,end)['items']
        name=wechat.names.get(user,user)
    if format=='markdown':data=exporting.markdown(items,user,name)
    elif format=='json':data=json.dumps(items,ensure_ascii=False,indent=2)
    else:data=ai.transcript(items)
    extension,mime={'json':('json','application/json'),'markdown':('md','text/markdown'),'txt':('txt','text/plain')}[format]
    return Response(data,media_type=mime,headers={'Content-Disposition':f'attachment; filename="chat.{extension}"'})

class IntegrationSettings(BaseModel):
    enabled:bool

class ClientPreview(BaseModel):
    clients:list[str]
    install_skill:bool=True
    replace_existing:bool=False

class ClientApply(BaseModel):
    token:str

def local_mcp_server():
    return integration.settings()['config']['mcpServers']['wechat-helper']

@app.get('/api/integration/clients')
def integration_clients():
    from .client_setup import discover
    return discover(local_mcp_server())

@app.post('/api/integration/clients/preview')
def preview_clients(body:ClientPreview):
    from .client_setup import preview
    try:return preview(body.clients,local_mcp_server(),body.install_skill,body.replace_existing)
    except (OSError,UnicodeError,ValueError) as error:
        raise HTTPException(400,str(error) if isinstance(error,ValueError) and not isinstance(error,UnicodeError) else '配置不可读取，请检查文件权限或编码') from None

@app.post('/api/integration/clients/apply')
def apply_clients(body:ClientApply):
    from .client_setup import apply
    try:return apply(body.token,local_mcp_server())
    except (OSError,ValueError) as error:
        raise HTTPException(409,str(error) if isinstance(error,ValueError) else '配置不可读取，请重新检测') from None

@app.get('/api/integration/settings')
def integration_settings():return integration.settings()

@app.post('/api/integration/settings')
def configure_integration(body:IntegrationSettings):
    integration.configure(body.enabled)
    return integration.settings()

@app.get('/api/integration/skill')
def integration_skill():
    return Response(SKILL.read_text('utf-8'),media_type='text/markdown',headers={'Content-Disposition':'attachment; filename="SKILL.md"'})

def external_ready():
    if wechat.status['state']!='ready':raise HTTPException(409,'请先在微信对话工作台连接账号并完成读取')

@app.get('/api/external/status')
def external_status():
    # Database errors/status text can contain account directories, so keep it generic.
    return {'state':wechat.status['state'],'generation':wechat.status['generation'],
            'message':'工作台已就绪' if wechat.status['state']=='ready' else '请在工作台检查连接状态',
            'workspace_count':len(wechat.workspace())}

@app.get('/api/external/sessions')
def external_sessions(q:str='',offset:int=Query(0,ge=0,le=1000000),limit:int=Query(50,ge=1,le=200)):
    external_ready()
    items=wechat.workspace_sessions(q)
    return {'items':items[offset:offset+limit],'total':len(items),'offset':offset,'has_more':offset+limit<len(items)}

@app.get('/api/external/messages')
def external_messages(user:str=Query(min_length=1),offset:int=Query(0,ge=0,le=1000000),limit:int=Query(100,ge=1,le=500),q:str='',start:int=Query(0,ge=0),end:int=Query(0,ge=0),format:Literal['json','markdown']='json'):
    external_ready()
    if end and end<start:raise HTTPException(422,'结束时间不能早于开始时间')
    with wechat.lock:
        workspace_ready(user)
        result=wechat.messages(user,offset,limit,q,start,end)
        name=wechat.names.get(user,user)
        generation=wechat.status['generation']
    result['generation']=generation
    result['next_offset']=offset+len(result['items']) if result['has_more'] else None
    if format=='markdown':
        result['markdown']=exporting.markdown(result.pop('items'),user,name)
    return result

@app.get('/api/external/context')
def external_context(user:str=Query(min_length=1)):
    with wechat.lock:
        workspace_ready(user)
        old=chat_memory.read(wechat,'summaries').get(user,[])
        account=memory_store.account_key(wechat.root)
        return {'note':wechat.workspace()[user].get('note',''),'profile':profile(user),'latest_summary':old[-1] if old else None,
                'knowledge':knowledge.confirmed_context(wechat,user),
                'period_summaries':memory_store.journals(account,set(wechat.workspace()),user,limit=12)}

@app.get('/api/image')
def image(user:str,local_id:int,database:str):
    from .media import image_for
    try:
        with wechat.lock:
            workspace_ready(user)
            data,mime=image_for(user,local_id,database,reader=wechat)
            return Response(data,media_type=mime)
    except (ValueError,OSError) as e:raise HTTPException(404,str(e))

@app.post('/api/image-key')
def image_key():
    from .media import find_image_key
    try:return find_image_key()
    except Exception as e:raise HTTPException(400,str(e))

@app.get('/api/ai/config')
def get_config(): return ai.config(True)

@app.post('/api/ai/config')
async def set_config(request:Request):
    try: return ai.save_config(await request.json())
    except (ValueError,TypeError) as e: raise HTTPException(400,str(e))

@app.post('/api/ai/test')
async def test_ai():
    try:
        result=await ai.complete([{'role':'user','content':'请只回复：连接成功'}])
        return {'ok':True,'response':result}
    except Exception as e: raise HTTPException(502,str(e))

@app.get('/api/profile')
def profile(user:str):
    workspace_ready(user)
    return chat_memory.read(wechat,'profiles').get(user,{'relationship':'朋友','style':'自然、简洁','background':'','goals':'','boundaries':''})

@app.post('/api/profile')
async def save_profile(request:Request):
    body=await request.json()
    with wechat.lock:
        workspace_ready(body['user']);data=chat_memory.read(wechat,'profiles')
        data[body['user']]={k:str(v) for k,v in body['profile'].items() if k in {'relationship','style','background','goals','boundaries'}}
        chat_memory.write(wechat,'profiles',data); return {'ok':True}

@app.get('/api/history')
def history(user:str):
    workspace_ready(user)
    return chat_memory.read(wechat,'summaries').get(user,[])

class Analysis(BaseModel):
    user:str
    action:str='advice'
    scope:str='recent'
    scenario:str=''
    start:int=0
    end:int=0

@app.post('/api/ai/run')
async def run_ai(body:Analysis):
    with wechat.lock:
        workspace_ready(body.user)
        account=wechat.root
    if body.action not in {'summary','advice'} or body.scope not in {'recent','all','range'}:
        raise HTTPException(400,'无效分析范围')
    key=uuid.uuid4().hex
    jobs[key]={'state':'running','message':'准备聊天上下文','result':None,'user':body.user,'account':str(account)}
    def check_scope():
        if wechat.root!=account:raise ValueError('账号已切换，分析已停止')
        workspace_ready(body.user)
    def scoped_read(fn,*args):
        with wechat.lock:
            check_scope()
            return fn(*args)
    def progress(message):
        scoped_read(lambda:jobs[key].update(message=message))
    async def work():
        spool=None
        try:
            limit=2**31-1 if body.scope in {'all','range'} and body.action=='summary' else ai.config()['context_messages']
            context=scoped_read(profile,body.user); old=scoped_read(history,body.user)
            context['note']=scoped_read(lambda:wechat.workspace()[body.user].get('note',''))
            context['knowledge']=scoped_read(knowledge.confirmed_context,wechat,body.user)
            context['period_summaries']=scoped_read(lambda:[{'period':j['period'],'label':j['label'],'text':j['text'][:1500]} for j in memory_store.journals(memory_store.account_key(wechat.root),set(wechat.workspace()),body.user,limit=3)])
            if body.action=='summary':
                if body.scope in {'all','range'}:
                    spool_task=asyncio.create_task(asyncio.to_thread(scoped_read,wechat.spool_transcript,body.user,body.start,body.end))
                    try:
                        spool,metadata=await asyncio.shield(spool_task)
                    except asyncio.CancelledError:
                        spool,metadata=await spool_task
                        raise
                    if not metadata['count']:raise ValueError('所选范围没有聊天记录')
                    def batches():
                        with spool.open(encoding='utf-8') as stream:
                            while chunk:=stream.read(ai.config()['chunk_chars']):yield chunk
                    output=await ai.summarize_batches(batches(),context,progress)
                else:
                    result=await asyncio.to_thread(scoped_read,wechat.messages,body.user,0,limit,'',body.start,body.end);items=result['items']
                    if not items:raise ValueError('所选范围没有聊天记录')
                    metadata={'count':len(items),'first':items[0]['timestamp'],'last':items[-1]['timestamp']}
                    output=await ai.summarize(items,context,progress)
                saved={'text':output,'created_at':int(time.time()),**metadata,'scope':body.scope}
                with wechat.lock:
                    check_scope()
                    data=chat_memory.read(wechat,'summaries'); data.setdefault(body.user,[]).append(saved)
                    chat_memory.write(wechat,'summaries',data)
            else:
                result=await asyncio.to_thread(scoped_read,wechat.messages,body.user,0,limit,'',body.start,body.end);items=result['items']
                if not items:raise ValueError('所选范围没有聊天记录')
                metadata={'count':len(items)}
                output=await ai.advise(items,context,old[-1]['text'] if old else '',body.scenario)
            with wechat.lock:
                check_scope()
                jobs[key].update(state='done',message=f"已分析 {metadata['count']} 条消息",result=output,count=metadata['count'])
        except asyncio.CancelledError:jobs[key].update(state='cancelled',message='分析已取消')
        except Exception as e: jobs[key].update(state='error',message=str(e))
        finally:
            if spool:spool.unlink(missing_ok=True)
            tasks.pop(key,None)
    tasks[key]=asyncio.create_task(work())
    # Completed job contents do not accumulate indefinitely in memory.
    if len(jobs)>100:
        for job_id in list(jobs):
            if jobs[job_id]['state']!='running' and job_id!=key:
                del jobs[job_id]
                if len(jobs)<=80: break
    return {'id':key}

@app.post('/api/jobs/{job_id}/cancel')
async def cancel(job_id:str):
    task=tasks.get(job_id)
    if task:task.cancel()
    return {'ok':True}

@app.get('/api/jobs/{job_id}')
def job(job_id:str):
    if job_id not in jobs: raise HTTPException(404,'任务不存在')
    with wechat.lock:
        if jobs[job_id].get('account')!=str(wechat.root):raise HTTPException(403,'分析所属账号已切换')
        workspace_ready(jobs[job_id]['user'])
    return jobs[job_id]

def monitor():
    if wechat.current.get('path'):
        wechat.connect(wechat.current['path'])
    while not stop.wait(2):
        if wechat.root and wechat.status['state']=='ready':
            try: wechat.sync()
            except Exception as e: wechat.status.update(message=str(e))

product_api.install(app,lambda:wechat)
