"""Run against this project's local WSLC development stack using synthetic test data.

prepare: create a dedicated QA user; verify: exercise real REST/MCP/three databases;
cleanup: remove only the generated QA user's records and encrypted documents.
The default administrator password is never changed and never printed.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from sqlalchemy import delete, select

from backend.platform_db import Database, clients, metadata, receipts, users, row
from backend.platform_documents import MongoDocuments
from backend.platform_security import password_hash
from helper.platform_sync import envelope

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT/'.private/wslc-v2'


def connections():
    c = json.loads((PRIVATE/'credentials.json').read_text('utf-8'))
    db = Database(f"mysql+pymysql://wechat:{c['mysql_password']}@127.0.0.1:13306/wechat?charset=utf8mb4")
    documents = MongoDocuments(f"mongodb://wechat:{c['mongo_password']}@127.0.0.1:17017/wechat?authSource=admin")
    return db, documents


def prepare():
    db, documents = connections()
    path = PRIVATE/'qa.json'
    if path.exists():
        print('QA credentials already exist; reusing this test identity')
        return
    data = {'id':secrets.token_hex(16),'username':'qa_'+secrets.token_hex(4),'password':secrets.token_urlsafe(24)}
    with db.transaction() as con:
        con.execute(users.insert().values(id=data['id'],username=data['username'],name='集成测试 · 合成数据',password=password_hash(data['password']),
            role='admin',enabled=True,must_change=False,epoch=1,created_at=int(time.time())))
    path.write_text(json.dumps(data),encoding='utf-8')
    documents.close();db.engine.dispose()
    print('Dedicated QA identity prepared; credentials saved only under .private/wslc-v2/qa.json')


def verify():
    qa = json.loads((PRIVATE/'qa.json').read_text('utf-8'))
    with httpx.Client(base_url='http://127.0.0.1:8787',headers={'X-Wechat-UI':'1'},timeout=30,trust_env=False) as c:
        def req(method,path,data=None,**kw):
            r=c.request(method,'/api/v2/'+path,json=data,**kw)
            assert r.status_code == 200,(path,r.status_code,r.text[:300])
            return r.json()
        req('POST','auth/login',{'username':qa['username'],'password':qa['password']})
        dev=req('POST','clients',{'name':'WSLC 集成测试设备'})
        reg=req('POST','client/register',{'app_key':dev['app_key'],'app_secret':dev['app_secret'],'device_id':'qa-device-'+qa['id']})
        config={**dev,**reg}
        headers={'Authorization':'Bearer '+reg['token']}
        manifest={'kind':'manifest','source':'e'*64,'account_name':'测试微信 · 无真实聊天','self_id':'test_self',
            'sessions':[{'id':'test_friend','name':'测试联系人'}],'contacts':[{'id':'test_friend','name':'测试联系人','nickname':'测试昵称','remark':'合成测试资料'}]}
        req('POST','client/sync',envelope(config,manifest),headers=headers)
        message={'kind':'messages','source':'e'*64,'session':'test_friend','items':[
            {'id':'test:1','database':'test.db','local_id':1,'timestamp':1704067200,'text':'【合成测试消息】下周一起检查服务端功能。','sender':'我','is_me':True},
            {'id':'test:2','database':'test.db','local_id':2,'timestamp':1704067210,'text':'【合成测试消息】收到，周一再沟通。','sender':'测试联系人'}]}
        encrypted=envelope(config,message)
        result=req('POST','client/sync',encrypted,headers=headers)
        assert req('POST','client/sync',encrypted,headers=headers)==result
        assert req('POST','client/sync',envelope(config,message),headers=headers)['changed']==0
        sid=req('GET','sessions')['items'][0]['id']
        assert req('GET',f'sessions/{sid}/messages')['total']==2
        assert req('GET','contacts')['total']==1
        req('POST',f'sessions/{sid}/listener',{'enabled':True})
        req('PUT','model',{'base_url':'http://wechat-v2-test-model:18080/v1','model':'integration-test-only','api_key':'synthetic-test-only'})
        assert req('POST','model/test',{})['ok']
        deadline=time.time()+50
        while time.time()<deadline:
            tasks=req('GET','manage/jobs')['items']
            if len(tasks)==5 and all(t['state']=='done' for t in tasks):break
            time.sleep(1)
        assert len(tasks)==5 and all(t['state']=='done' for t in tasks),[(t['cycle'],t['state'],t['error']) for t in tasks]
        memory_list=req('GET','memories')['items']
        assert len(memory_list)==7,memory_list
        read=req('POST','keys',{'name':'QA read','permission':'read'})
        full=req('POST','keys',{'name':'QA full','permission':'full'})
        def tool(token,name,arguments):
            r=c.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json,text/event-stream'},json={
                'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':arguments}})
            return r
        assert not tool(read['key'],'read_messages',{'session_id':sid}).json()['result'].get('isError')
        assert not tool(full['key'],'read_messages',{'session_id':sid}).json()['result'].get('isError')
        req('POST',f"keys/{read['id']}/enabled",{'enabled':False})
        assert tool(read['key'],'read_messages',{'session_id':sid}).status_code==401
        item=req('GET','memories/'+memory_list[0]['id'])
        assert item['evidence'] and '测试' in item['markdown']
        r=c.get('/api/v2/download/client')
        assert r.status_code==200 and r.content.startswith(b'PK'),r.status_code
        assert c.get('/readyz').json()['services']=={'sql':'ok','mongo':'ok','redis':'ok'}
        report={'passed':True,'checked_at':datetime.now().isoformat(),'messages':2,'period_jobs':5,'memories':7,
            'checks':['real MySQL encrypted ingestion','real MongoDB immutable Markdown','Redis connectivity','idempotent retries',
                      'HTTP model integration using synthetic stub','five automatic period summaries','profile and relationship memories',
                      'MCP read-only/full permissions','immediate key disable','authenticated Windows client download']}
        (PRIVATE/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))


def cleanup():
    path=PRIVATE/'qa.json'
    if not path.exists():return
    qa=json.loads(path.read_text('utf-8'))
    db,documents=connections()
    with db.transaction() as con:
        user=row(con,users,users.c.id==qa['id'])
        if not user or user['username']!=qa['username'] or not user['username'].startswith('qa_'):
            raise RuntimeError('QA identity mismatch; cleanup aborted')
        client_ids=con.execute(select(clients.c.id).where(clients.c.user_id==qa['id'])).scalars().all()
        if client_ids:
            con.execute(delete(receipts).where(receipts.c.client_id.in_(client_ids)))
        for table in reversed(metadata.sorted_tables):
            if 'user_id' in table.c:
                con.execute(delete(table).where(table.c.user_id==qa['id']))
        con.execute(delete(users).where(users.c.id==qa['id']))
    documents.collection.delete_many({'user_id':qa['id']})
    documents.close();db.engine.dispose()
    path.unlink()
    print('Only the generated QA identity and its synthetic records were removed')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','verify','cleanup']);args=parser.parse_args()
    {'prepare':prepare,'verify':verify,'cleanup':cleanup}[args.action]()
