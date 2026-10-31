"""Account-scoped, transactional memory, journals, events and automation history."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from . import storage

CATEGORIES = {'identity', 'personality', 'preferences', 'style', 'relationship', 'boundaries', 'goals'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS facts (
 id TEXT PRIMARY KEY, account TEXT NOT NULL, user TEXT NOT NULL, subject TEXT NOT NULL,
 category TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL, certainty TEXT NOT NULL,
 evidence TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS facts_scope ON facts(account,user,subject,status);
CREATE TABLE IF NOT EXISTS revisions (
 id INTEGER PRIMARY KEY, fact_id TEXT NOT NULL, previous TEXT NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS journals (
 id TEXT PRIMARY KEY, account TEXT NOT NULL, user TEXT NOT NULL, period TEXT NOT NULL,
 start INTEGER NOT NULL, end INTEGER NOT NULL, label TEXT NOT NULL, text TEXT NOT NULL,
 count INTEGER NOT NULL, digest TEXT NOT NULL, created_at INTEGER NOT NULL,
 UNIQUE(account,user,period,start,end));
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, account TEXT NOT NULL, user TEXT NOT NULL, title TEXT NOT NULL,
 start TEXT NOT NULL, end TEXT NOT NULL, all_day INTEGER NOT NULL, recurrence TEXT NOT NULL,
 description TEXT NOT NULL, location TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL,
 sync_state TEXT NOT NULL DEFAULT 'pending', remote_url TEXT NOT NULL DEFAULT '',
 remote_etag TEXT NOT NULL DEFAULT '', sync_error TEXT NOT NULL DEFAULT '',
 updated_at INTEGER NOT NULL, created_at INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS event_scope ON events(account,user,status);
CREATE TABLE IF NOT EXISTS settings (account TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(account,name));
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, account TEXT NOT NULL, user TEXT NOT NULL, period TEXT NOT NULL,
 label TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL,
 started_at INTEGER NOT NULL, finished_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS leases (account TEXT PRIMARY KEY, owner TEXT NOT NULL, expires INTEGER NOT NULL);
'''


def account_key(root):
    if root is None:
        raise ValueError('请先连接微信账号')
    return hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()


@contextmanager
def database():
    connection = sqlite3.connect(storage.DATA/'memory.sqlite3', timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA busy_timeout=30000')
        connection.executescript(SCHEMA)
        if 'revision' not in {row[1] for row in connection.execute('PRAGMA table_info(events)')}:
            connection.execute('ALTER TABLE events ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
        with connection:
            yield connection
    finally:
        connection.close()


def unpack(row):
    result = dict(row)
    if 'evidence' in result:
        result['evidence'] = json.loads(result['evidence'])
    result.pop('account', None)
    return result


def facts(account, users, user=None, subject='other', include_rejected=False):
    if subject not in {'self','other'}:
        raise ValueError('无效画像范围')
    with database() as con:
        rows=con.execute('SELECT * FROM facts WHERE account=? AND subject=? ORDER BY updated_at DESC', (account,subject)).fetchall()
    result=[]
    for row in rows:
        item=unpack(row)
        if not include_rejected and item['status']=='rejected':continue
        if subject=='other' and (item['user'] not in users or user is not None and item['user']!=user):continue
        # Self knowledge follows its evidence's current workspace membership.
        visible=[e for e in item['evidence'] if e.get('user') in users]
        if subject=='self' and item['evidence'] and not visible:continue
        item['evidence']=visible
        result.append(item)
    return result


def save_fact(account, users, value, fact_id=None):
    now=int(time.time())
    with database() as con:
        old=con.execute('SELECT * FROM facts WHERE account=? AND id=?',(account,fact_id)).fetchone() if fact_id else None
        if fact_id and old is None:raise ValueError('知识条目不存在')
        if old and old['subject']=='other' and old['user'] not in users:raise ValueError('该会话已移出工作台')
        if old and old['subject']=='self':
            evidence=json.loads(old['evidence'])
            if evidence and not any(e.get('user') in users for e in evidence):raise ValueError('知识来源已移出工作台')
        subject=old['subject'] if old else value.get('subject','other')
        user=old['user'] if old else value.get('user','')
        if subject not in {'self','other'}:raise ValueError('无效画像范围')
        if subject=='other' and user not in users:raise ValueError('请先将会话加入工作台')
        text=str(value.get('text',old['text'] if old else '')).strip()
        category=value.get('category',old['category'] if old else 'identity')
        status=value.get('status','confirmed')
        if not text or len(text)>2000 or category not in CATEGORIES or status not in {'pending','confirmed','rejected'}:
            raise ValueError('请检查条目内容、类别和状态')
        key=fact_id or uuid.uuid4().hex
        if old:
            con.execute('INSERT INTO revisions(fact_id,previous,updated_at) VALUES (?,?,?)',(key,json.dumps(unpack(old),ensure_ascii=False),now))
            con.execute('UPDATE facts SET text=?,category=?,status=?,updated_at=? WHERE id=?',(text,category,status,now,key))
        else:
            con.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (key,account,user if subject=='other' else '__self__',subject,category,text,status,'manual','[]',now,now))
    return key


def journal(account,user,period,start,end):
    with database() as con:
        row=con.execute('SELECT * FROM journals WHERE account=? AND user=? AND period=? AND start=? AND end=?',(account,user,period,start,end)).fetchone()
    return unpack(row) if row else None


def journals(account,users,user=None,period=None,limit=100):
    with database() as con:
        rows=con.execute('SELECT * FROM journals WHERE account=? ORDER BY start DESC,created_at DESC',(account,)).fetchall()
    return [unpack(r) for r in rows if r['user'] in users and (user is None or r['user']==user) and (period is None or r['period']==period)][:limit]


def commit_learning(account,user,period,start,end,label,text,metadata,new_facts,new_events):
    """A completed learning run commits all derived data together; manual text wins."""
    now=int(time.time())
    with database() as con:
        journal_id=hashlib.sha256(f'{account}|{user}|{period}|{start}|{end}'.encode()).hexdigest()
        con.execute('INSERT INTO journals VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account,user,period,start,end) DO UPDATE SET text=excluded.text,count=excluded.count,digest=excluded.digest,created_at=excluded.created_at',
                    (journal_id,account,user,period,start,end,label,text,metadata['count'],metadata['digest'],now))
        for fact in new_facts:
            owner=user if fact['subject']=='other' else '__self__'
            key=hashlib.sha256(f"{account}|{owner}|{fact['category']}|{fact['text'].casefold()}".encode()).hexdigest()
            old=con.execute('SELECT evidence FROM facts WHERE id=?',(key,)).fetchone()
            evidence=json.loads(old['evidence']) if old else []
            evidence_by_id={(e['user'],e['id']):e for e in evidence+fact['evidence']}
            combined=json.dumps(list(evidence_by_id.values())[-100:],ensure_ascii=False)
            con.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET evidence=excluded.evidence,updated_at=excluded.updated_at',
                        (key,account,owner,fact['subject'],fact['category'],fact['text'],'pending',fact['certainty'],combined,now,now))
        for event in new_events:
            key=hashlib.sha256(f"{account}|{user}|{event['title'].casefold()}|{event['start']}".encode()).hexdigest()
            con.execute('INSERT OR IGNORE INTO events(id,account,user,title,start,end,all_day,recurrence,description,location,status,evidence,updated_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (key,account,user,event['title'],event['start'],event['end'],event['all_day'],event['recurrence'],event['description'],event['location'],'draft',json.dumps(event['evidence'],ensure_ascii=False),now,now))
    return journal_id


def get_setting(account,name,default):
    with database() as con:row=con.execute('SELECT value FROM settings WHERE account=? AND name=?',(account,name)).fetchone()
    return json.loads(row['value']) if row else default


def set_setting(account,name,value):
    with database() as con:con.execute('INSERT INTO settings VALUES (?,?,?) ON CONFLICT(account,name) DO UPDATE SET value=excluded.value',(account,name,json.dumps(value,ensure_ascii=False)))


def run_started(account,user,period,label):
    key=uuid.uuid4().hex
    with database() as con:con.execute('INSERT INTO runs(id,account,user,period,label,state,message,started_at) VALUES (?,?,?,?,?,?,?,?)',(key,account,user,period,label,'running','准备读取所选会话',int(time.time())))
    return key


def finish_run(key,state,message):
    with database() as con:con.execute('UPDATE runs SET state=?,message=?,finished_at=? WHERE id=?',(state,message,int(time.time()),key))


def recent_runs(account,users):
    with database() as con:rows=con.execute('SELECT * FROM runs WHERE account=? ORDER BY started_at DESC,rowid DESC LIMIT 100',(account,)).fetchall()
    return [unpack(r) for r in rows if not r['user'] or r['user'] in users]


def acquire_lease(account,owner,seconds=180):
    with database() as con:
        now=int(time.time())
        con.execute('DELETE FROM leases WHERE expires<?',(now,))
        return bool(con.execute('INSERT INTO leases VALUES (?,?,?) ON CONFLICT(account) DO UPDATE SET expires=excluded.expires WHERE owner=excluded.owner',(account,owner,now+seconds)).rowcount)


def release_lease(account,owner):
    with database() as con:con.execute('DELETE FROM leases WHERE account=? AND owner=?',(account,owner))
