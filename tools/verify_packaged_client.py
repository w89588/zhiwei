"""Launch the built Windows client with a new isolated data directory and synthetic WeChat DBs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import httpx

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--application',type=Path,default=ROOT/'dist/v2-dev/WechatHelper/WechatHelper.exe')
parser.add_argument('--archive',type=Path,default=ROOT/'release/WechatHelper-2.0-dev.zip')
args=parser.parse_args()
qa=json.loads((ROOT/'.private/wslc-v2/qa.json').read_text('utf-8'))
run_root=ROOT/'.private'/('client-smoke-'+str(time.time_ns()))
data=run_root/'local-data';data.mkdir(parents=True)
source=run_root/'wxid_synthetic_abcd'/'db_storage'
for name in ['contact','session','message']:(source/name).mkdir(parents=True)
with sqlite3.connect(source/'contact/contact.db') as con:
    con.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,local_type INTEGER DEFAULT 1,flag INTEGER DEFAULT 3,verify_flag INTEGER DEFAULT 0,delete_flag INTEGER DEFAULT 0)')
    con.executemany('INSERT INTO contact(username,nick_name,remark) VALUES (?,?,?)',[('friend','合成测试联系人',''),('wxid_synthetic','测试本人','')])
with sqlite3.connect(source/'session/session.db') as con:
    con.execute('CREATE TABLE SessionTable(username TEXT,unread_count INTEGER,summary TEXT,last_timestamp INTEGER)')
    con.execute('INSERT INTO SessionTable VALUES (?,?,?,?)',('friend',0,'合成消息',1704067200))
with sqlite3.connect(source/'message/message_0.db') as con:
    con.execute('CREATE TABLE Name2Id(user_name TEXT)')
    con.executemany('INSERT INTO Name2Id VALUES (?)',[('wxid_synthetic',),('friend',)])
    table='Msg_'+hashlib.md5(b'friend').hexdigest()
    con.execute(f'CREATE TABLE [{table}](local_id INTEGER,real_sender_id INTEGER,local_type INTEGER,create_time INTEGER,message_content TEXT)')
    con.executemany(f'INSERT INTO [{table}] VALUES (?,?,?,?,?)',[(1,1,1,1704067200,'仅用于打包验证的测试消息'),(2,2,1,1704067201,'客户端同步测试')])
env={**os.environ,'WECHAT_HELPER_DATA':str(data)}
process=subprocess.Popen([str(args.application.resolve()),'--browser','--port','18766'],env=env,
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
try:
    with httpx.Client(base_url='http://127.0.0.1:18766',trust_env=False,timeout=20) as local, httpx.Client(base_url='http://127.0.0.1:8787',trust_env=False,timeout=20,headers={'X-Wechat-UI':'1'}) as remote:
        for _ in range(35):
            if process.poll() is not None:raise AssertionError('Packaged client exited; inspect isolated startup-error.log')
            try:
                page=local.get('/')
                if page.status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(1)
        token=re.search(r"window.HELPER_TOKEN='([^']+)'",page.text).group(1)
        local.headers['X-Helper-Token']=token
        assert 'platform-sync.js' in page.text
        assert local.post('/api/connect',json={'path':str(source)}).status_code==200
        for _ in range(20):
            status=local.get('/api/status').json()
            if status['state']=='ready':break
            time.sleep(.5)
        assert status['state']=='ready',status['state']
        assert remote.post('/api/v2/auth/login',json={'username':qa['username'],'password':qa['password']}).status_code==200
        credentials=remote.post('/api/v2/clients',json={'name':'Windows 打包验证（合成数据）'}).json()
        response=local.post('/api/platform/settings',json={'url':'http://127.0.0.1:8787','app_key':credentials['app_key'],
            'app_secret':credentials['app_secret'],'enabled':True,'scope':'all'})
        assert response.status_code==200,response.text
        assert local.post('/api/platform/test',json={}).status_code==200
        assert local.post('/api/platform/sync',json={}).status_code==200
        for _ in range(30):
            sync=local.get('/api/platform/settings').json()
            if sync['status']['state']=='done':break
            if sync['status']['state']=='error':raise AssertionError(sync['status']['message'])
            time.sleep(.5)
        assert sync['status']['state']=='done',sync['status']
        assert credentials['app_secret'].encode() not in (data/'platform-sync.bin').read_bytes()
        accounts=remote.get('/api/v2/accounts').json()['items']
        account=next(a for a in accounts if a['name']=='wxid_synthetic_abcd')
        catalog=remote.get('/api/v2/sessions',params={'account_id':account['id']}).json()['items']
        assert any(item['message_count']==2 for item in catalog),catalog
        result={'packaged_exe_started':True,'real_sqlite_contacts_parsed':True,'encrypted_upload_to_wslc':True,'messages':2,'dpapi_secrets_verified':True,
            'archive_sha256':hashlib.file_digest(args.archive.open('rb'),'sha256').hexdigest()}
        (ROOT/'.private/wslc-v2/packaged-client-verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result))
finally:
    process.terminate()
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
