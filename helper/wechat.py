import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import heapq
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET
import zstandard
from .crypto import HEADER, find_keys, snapshot
from .storage import DATA, read_json, write_json
from .sync_scope import CONTACT_FIELDS, is_message_database, is_sync_contact, is_sync_session

def discover():
    roots = [Path.home()/'Documents'/'xwechat_files', Path.home()/'xwechat_files']
    config = Path(os.environ.get('APPDATA',''))/'Tencent'/'xwechat'/'config'
    if config.exists():
        for file in config.glob('*.ini'):
            try:
                value = file.read_text(encoding='utf-8').strip('\x00\r\n ')
                roots.append(Path(value)/'xwechat_files')
            except (OSError,UnicodeError):
                pass
    results = []
    for root in dict.fromkeys(roots):
        if root.exists():
            for account in root.iterdir():
                db = account/'db_storage'
                if db.is_dir():
                    results.append({'account':account.name,'path':str(db)})
    return results

def decode(value):
    if isinstance(value,bytes):
        try:
            value = zstandard.ZstdDecompressor().decompress(value,max_output_size=16*1024*1024)
        except zstandard.ZstdError:
            pass
        return value.decode('utf-8',errors='replace')
    return str(value or '')

TYPES = {1:'文本',3:'图片',34:'语音',42:'名片',43:'视频',47:'表情',48:'位置',49:'链接 / 文件',50:'通话',10000:'系统',10002:'撤回'}

def parse_content(raw, kind, group=False):
    text = decode(raw)
    sender = ''
    if group and ':\n' in text and not text.lstrip().startswith('<'):
        sender,text = text.split(':\n',1)
    extra = {}
    if kind != 1:
        try:
            xml = ET.fromstring(text)
            # App-message fields must come from the outer appmsg; a quoted or
            # forwarded message may contain another title or URL of its own.
            app = (xml if xml.tag == 'appmsg' else xml.find('.//appmsg')) if kind == 49 else None
            for tag in ['title','des','url','filename','refermsg/content','refermsg/displayname']:
                value = app.findtext(tag) if app is not None else xml.findtext('.//'+tag)
                if value:
                    extra[tag] = value
            if app is not None and not extra.get('filename'):
                value = app.findtext('appattach/filename')
                if value:
                    extra['filename'] = value
            duration = xml.find('.//voicemsg')
            if duration is not None:
                extra['duration_ms'] = duration.attrib.get('voicelength','')
            # WeChat stores media details in attributes and app attachment
            # elements, not necessarily in the message title.
            for element, attributes in (
                ('img', ('length', 'width', 'height')),
                ('videomsg', ('length', 'playlength', 'width', 'height')),
                ('voicemsg', ('length', 'voicelength')),
            ):
                media = xml.find('.//' + element)
                if media is not None:
                    for attribute in attributes:
                        value = media.attrib.get(attribute, '')
                        if value.isdecimal() and len(value) <= 12:
                            extra[element + '_' + attribute] = int(value)
            for tag in ('appattach/totallen', 'appattach/fileext'):
                value = xml.findtext('.//' + tag)
                if value:
                    extra[tag] = value[:100]
            text = extra.get('title') or extra.get('des') or '['+TYPES.get(kind,str(kind))+']'
            if extra.get('refermsg/content'):
                text += '\n引用：'+extra.get('refermsg/displayname','')+' '+extra['refermsg/content']
        except ET.ParseError:
            if kind not in (10000,10002):
                text = '['+TYPES.get(kind,str(kind))+']'+ (' '+text if not text.startswith('<') else '')
    return sender,text,extra

class Wechat:
    def __init__(self):
        self.lock = threading.RLock()
        self.root = None
        self.keys = {}
        self.cache = {}
        self.names = {}
        self.tables = {}
        self._sync_contacts_cache = None
        self._sender_cache = {}
        self.status = {'state':'idle','message':'连接本机微信，开始浏览聊天','generation':0,'errors':[]}
        self.current = read_json('source.json',{})

    def workspace(self):
        """Selections and notes are isolated by the canonical account directory."""
        if not self.root:
            return {}
        account = os.path.normcase(str(self.root.resolve()))
        return read_json('workspace.json',{}).get(account,{})

    def in_workspace(self,user):
        with self.lock:
            return user in self.workspace()

    def update_workspace(self,user,selected,note=None):
        with self.lock:
            if not self.root or self.status['state']!='ready':
                raise ValueError('请先连接微信账号')
            data=read_json('workspace.json',{})
            account=os.path.normcase(str(self.root.resolve()))
            entries=data.setdefault(account,{})
            if selected:
                if user not in entries and user not in {s['id'] for s in self.sessions()}:
                    raise ValueError('未找到该会话，请刷新会话列表')
                entry=entries.setdefault(user,{'note':''})
                if note is not None:entry['note']=note.strip()
            else:
                entries.pop(user,None)
            write_json('workspace.json',data)
            self.status['generation']+=1
            if not entries:
                for rel in list(self.cache):
                    if rel.startswith('message/'):
                        self.cache.pop(rel,None);self.tables.pop(rel,None)

    def workspace_sessions(self,query=''):
        with self.lock:
            entries=self.workspace()
            if not entries:return []
            catalog={s['id']:s for s in self.sessions()}
            results=[]
            for user,entry in entries.items():
                session=dict(catalog.get(user,{'id':user,'name':self.names.get(user,user),'unread':0,'preview':'暂无本机消息','timestamp':0,'group':'@chatroom' in user}))
                session['note']=entry.get('note','')
                if query.casefold() in (session['name']+user+session['note']).casefold():results.append(session)
            return sorted(results,key=lambda s:s['timestamp'] or 0,reverse=True)

    def files(self):
        if not self.root:
            return []
        from .platform_sync import platform_uploader
        selected=bool(self.workspace()) or platform_uploader.allow_all(self)
        return sorted([p for p in self.root.rglob('*.db') if p.name in {'contact.db','session.db'} or selected and (p.name=='message_resource.db' or re.fullmatch(r'message_\d+\.db',p.name))])

    def connect(self,path,scan=False,persist=True):
        with self.lock:
            self.status.update(state='connecting',message='正在验证账号和数据库密钥',errors=[])
            try:
                root = Path(path).resolve()
                if not (root/'contact'/'contact.db').exists():
                    raise ValueError('请选择包含 contact、session、message 的 db_storage 目录')
                if root != self.root:
                    self.cache.clear(); self.tables.clear(); self.names.clear()
                    self._sync_contacts_cache = None
                    self._sender_cache.clear()
                self.root = root
                saved = read_json('keys.bin',{},True)
                self.keys = saved.get(str(root),{})
                first = (root/'contact'/'contact.db').read_bytes()[:16]
                if scan or (not self.keys and first != HEADER):
                    self.keys = find_keys(root,lambda msg:self.status.update(message=msg))
                    saved[str(root)] = self.keys
                    write_json('keys.bin',saved,True)
                self.current = {'path':str(root)}
                if persist:write_json('source.json',self.current)
                self.sync(force=True)
                if 'contact/contact.db' not in self.cache:
                    raise RuntimeError('联系人数据库读取失败；请重新提取密钥')
            except Exception as error:
                self.status.update(state='error',message=str(error))

    @staticmethod
    def fingerprint(path):
        try:
            stat = path.stat()
            return stat.st_mtime_ns,stat.st_size
        except FileNotFoundError:
            return 0,0

    def sync(self,force=False,priority_users=None,deadline=None):
        with self.lock:
            errors=[]; changed=False; complete=True; processed=0
            files = self.files()
            if not any(source.relative_to(self.root).as_posix() == 'contact/contact.db' for source in files):
                if self.cache.pop('contact/contact.db', None) is not None:
                    self.names.clear()
                    changed = True
                self.tables.pop('contact/contact.db', None)
                self._sync_contacts_cache = None
            if priority_users is not None:
                wanted = {'Msg_'+hashlib.md5(user.encode()).hexdigest() for user in priority_users}
                files = [source for source in files if source.relative_to(self.root).as_posix() in {'contact/contact.db','session/session.db'}
                    or (wanted and is_message_database(source.relative_to(self.root).as_posix()) and
                        (source.relative_to(self.root).as_posix() not in self.tables or
                         bool(wanted & self.tables[source.relative_to(self.root).as_posix()])))]
            for source in files:
                if deadline is not None and processed and time.monotonic() >= deadline:
                    complete=False
                    break
                rel = source.relative_to(self.root).as_posix()
                fingerprint = (self.fingerprint(source),self.fingerprint(Path(str(source)+'-wal')))
                if not force and rel in self.cache and self.cache[rel][0] == fingerprint:
                    continue
                processed += 1
                target = DATA/'cache'/hashlib.sha256(str(self.root).encode()).hexdigest()[:16]/rel
                target.parent.mkdir(parents=True,exist_ok=True)
                temp = target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
                try:
                    for attempt in range(3):
                        before = (self.fingerprint(source),self.fingerprint(Path(str(source)+'-wal')))
                        with source.open('rb') as stream:
                            salt = stream.read(16).hex()
                        key = bytes.fromhex(self.keys[salt]) if salt in self.keys else None
                        old=self.cache.get(rel)
                        base_cache=old[1] if old and old[0] and old[0][0]==before[0] else None
                        snapshot(source,temp,key,base_cache)
                        after = (self.fingerprint(source),self.fingerprint(Path(str(source)+'-wal')))
                        if before == after:
                            break
                    else:
                        raise RuntimeError('数据库持续写入，保留上次快照，下一轮重试')
                    con = sqlite3.connect(temp)
                    try:
                        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                        if source.name == 'contact.db':
                            self.names = {r[0]:(r[2] or r[1] or r[0]) for r in con.execute('SELECT username,nick_name,remark FROM contact')}
                    finally:
                        con.close()
                    temp.replace(target)
                    self.cache[rel] = (after,target)
                    self.tables[rel] = set(tables)
                    changed=True
                except Exception as error:
                    errors.append({'database':rel,'error':str(error)})
                    temp.unlink(missing_ok=True)
            self.status.update(state='ready' if self.cache else 'error',message='实时同步中' if not errors else '部分数据库同步失败，详情见连接状态',errors=errors)
            if changed:
                self.status['generation'] += 1
            return complete

    def connect_db(self,rel):
        con = sqlite3.connect(f'file:{self.cache[rel][1].as_posix()}?mode=ro',uri=True)
        con.row_factory = sqlite3.Row
        return con

    def sessions(self,query=''):
        with self.lock:
            sessions = {}
            rel = 'session/session.db'
            if rel in self.cache:
                con = self.connect_db(rel)
                try:
                    for row in con.execute('SELECT username,unread_count,summary,last_timestamp FROM SessionTable'):
                        sessions[row['username']] = {'id':row['username'],'name':self.names.get(row['username'],row['username']),'unread':row['unread_count'],'preview':decode(row['summary']),'timestamp':row['last_timestamp'],'group':'@chatroom' in row['username']}
                finally:
                    con.close()
            # Contact metadata lets users add archived chats without opening message shards.
            account=self.root.parent.name.rsplit('_',1)[0] if self.root else ''
            for user,name in self.names.items():
                if user!=account and user not in sessions:
                    sessions[user]={'id':user,'name':name,'unread':0,'preview':'联系人 · 暂无会话预览','timestamp':0,'group':'@chatroom' in user}
            # Include archived conversations absent from the current session list.
            for rel,tables in self.tables.items():
                if not is_message_database(rel) or 'Name2Id' not in tables:
                    continue
                con = self.connect_db(rel)
                try:
                    for row in con.execute('SELECT user_name FROM Name2Id'):
                        user=row[0]
                        if 'Msg_'+hashlib.md5(user.encode()).hexdigest() in tables and user not in sessions:
                            sessions[user]={'id':user,'name':self.names.get(user,user),'unread':0,'preview':'历史会话','timestamp':0,'group':'@chatroom' in user}
                finally:
                    con.close()
            return sorted([s for s in sessions.values() if query.lower() in (s['name']+s['id']).lower()],key=lambda s:s['timestamp'] or 0,reverse=True)

    def sessions_for(self, users):
        """Build the ordinary session shape for selected IDs without scanning every chat."""
        with self.lock:
            selected = set(users)
            found = {}
            rel = 'session/session.db'
            if rel in self.cache and 'SessionTable' in self.tables.get(rel, set()):
                con = self.connect_db(rel)
                try:
                    ids = sorted(selected)
                    for offset in range(0, len(ids), 500):
                        chunk = ids[offset:offset+500]
                        placeholders = ','.join('?' for _ in chunk)
                        for row in con.execute(f'SELECT username,unread_count,summary,last_timestamp FROM SessionTable WHERE username IN ({placeholders})', chunk):
                            user = row['username']
                            found[user] = {'id':user,'name':self.names.get(user,user),'unread':row['unread_count'],
                                'preview':decode(row['summary']),'timestamp':row['last_timestamp'],'group':'@chatroom' in user}
                finally:
                    con.close()
            for user in selected - found.keys():
                found[user] = {'id':user,'name':self.names.get(user,user),'unread':0,
                    'preview':'联系人 · 暂无会话预览' if user in self.names else '历史会话',
                    'timestamp':0,'group':'@chatroom' in user}
            return sorted(found.values(), key=lambda s:s['timestamp'] or 0, reverse=True)

    def messages(self,user,offset=0,limit=100,query='',start=0,end=0):
        with self.lock:
            table = 'Msg_'+hashlib.md5(user.encode()).hexdigest()
            all_rows=[]; total=0
            for rel,tables in self.tables.items():
                if table not in tables:
                    continue
                con=self.connect_db(rel)
                try:
                    sender_map={r[0]:r[1] for r in con.execute('SELECT rowid,user_name FROM Name2Id')}
                    where='WHERE create_time >= ?'+ (' AND create_time <= ?' if end else '')
                    args=[start]+([end] if end else [])
                    if not query:
                        total+=con.execute(f'SELECT COUNT(*) FROM [{table}] {where}',args).fetchone()[0]
                    sql=f'SELECT * FROM [{table}] {where} ORDER BY create_time DESC,local_id DESC'
                    params=args[:]
                    if not query:
                        sql+=' LIMIT ?'; params.append(offset+limit)
                    for row in con.execute(sql,params):
                        item=dict(row)
                        kind=int(item['local_type'])&0xffffffff
                        content=item.get('message_content') or item.get('compress_content')
                        sender,text,extra=parse_content(content,kind,'@chatroom' in user)
                        if int(item['local_type']) != kind:
                            extra['local_type'] = str(item['local_type'])
                        if query and query.casefold() not in (text+'\n'+json.dumps(extra,ensure_ascii=False)).casefold():
                            continue
                        if query: total+=1
                        sender=sender_map.get(item.get('real_sender_id'),sender) or sender
                        account=self.root.parent.name.rsplit('_',1)[0]
                        me=sender==account
                        all_rows.append({'id':rel+':'+str(item['local_id']),'local_id':item['local_id'],'server_id':item.get('server_id'),'timestamp':item['create_time'],'type':kind,'type_name':TYPES.get(kind,str(kind)),'sender_id':sender,'sender':'我' if me else self.names.get(sender,sender or self.names.get(user,user)),'is_me':me,'text':text,'raw':decode(content),'extra':extra})
                        if query and len(all_rows)>2*(offset+limit):
                            all_rows.sort(key=lambda m:(m['timestamp'],m['local_id'],m['id']),reverse=True)
                            del all_rows[offset+limit:]
                finally:
                    con.close()
            all_rows.sort(key=lambda m:(m['timestamp'],m['local_id'],m['id']),reverse=True)
            rows=all_rows[offset:offset+limit]
            return {'items':list(reversed(rows)),'total':total,'offset':offset,'has_more':offset+limit<total}

    def contacts(self):
        with self.lock:
            if 'contact/contact.db' not in self.cache:
                return []
            con=self.connect_db('contact/contact.db')
            try:
                return [{'id':r['username'],'name':r['remark'] or r['nick_name'] or r['username'],
                         'nickname':r['nick_name'] or '', 'remark':r['remark'] or '', 'group':'@chatroom' in r['username']}
                        for r in con.execute('SELECT username,nick_name,remark FROM contact') if r['username']]
            finally:con.close()

    def sync_contacts(self):
        """Cache classification per contact snapshot, never infer friends from names."""
        with self.lock:
            snapshot = self.cache.get('contact/contact.db')
            if not snapshot or any(e['database'] == 'contact/contact.db' for e in self.status.get('errors', [])):
                raise ValueError('通讯录读取失败，已暂停本轮上传并将重试')
            cache_key = (self.root, snapshot)
            if self._sync_contacts_cache is None or self._sync_contacts_cache[0] != cache_key:
                con = self.connect_db('contact/contact.db')
                try:
                    columns = {r['name'] for r in con.execute('PRAGMA table_info(contact)')}
                    if not CONTACT_FIELDS <= columns:
                        raise ValueError('当前微信通讯录格式暂不支持安全筛选，已暂停同步')
                    contacts = {}
                    for row in con.execute('SELECT username,nick_name,remark,local_type,flag,verify_flag,delete_flag FROM contact'):
                        if is_sync_contact(row):
                            user = row['username']
                            contacts[user] = {'id': user, 'name': row['remark'] or row['nick_name'] or user,
                                             'nickname': row['nick_name'] or '', 'remark': row['remark'] or '',
                                             'group': user.endswith('@chatroom')}
                finally:
                    con.close()
                self._sync_contacts_cache = (cache_key, contacts)
            return self._sync_contacts_cache[1]

    def can_sync_session(self, user):
        with self.lock:
            return is_sync_session(user, self.sync_contacts())

    def upload_senders(self, database, con):
        snapshot = self.cache[database]
        key = (snapshot, self.fingerprint(snapshot[1]))
        cached = self._sender_cache.get(database)
        if cached is None or cached[0] != key:
            cached = (key, {r[0]: r[1] for r in con.execute('SELECT rowid,user_name FROM Name2Id')})
            self._sender_cache[database] = cached
        return cached[1]

    def upload_batch(self,user,database,after=-1,limit=200,platform=False,newest=False,media_only=False):
        """Read one shard with a stable keyset; never load full history into memory."""
        with self.lock:
            if platform:
                from .platform_sync import platform_uploader
                if not platform_uploader.allowed(self,user):raise ValueError('会话不在平台上传授权范围')
                if not is_message_database(database):return []
            elif not self.in_workspace(user):raise ValueError('会话已移出工作台')
            table='Msg_'+hashlib.md5(user.encode()).hexdigest()
            if table not in self.tables.get(database,set()):return []
            con=self.connect_db(database)
            try:
                senders=self.upload_senders(database, con)
                items=[]
                direction = 'DESC' if newest else 'ASC'
                media_filter = ' AND (local_type & 4294967295) IN (3,34,43,49)' if media_only else ''
                for row in con.execute(f'SELECT * FROM [{table}] WHERE local_id>?{media_filter} ORDER BY local_id {direction} LIMIT ?', (after,limit)):
                    row=dict(row);kind=int(row['local_type'])&0xffffffff
                    content=row.get('message_content') or row.get('compress_content')
                    sender,text,extra=parse_content(content,kind,'@chatroom' in user)
                    if int(row['local_type']) != kind:
                        extra['local_type'] = str(row['local_type'])
                    sender=senders.get(row.get('real_sender_id'),sender) or sender
                    me=sender==self.root.parent.name.rsplit('_',1)[0]
                    items.append({'id':f"{database}:{row['local_id']}",'database':database,
                                  'local_id':row['local_id'],'server_id':row.get('server_id'),
                                  'timestamp':row['create_time'],'type':kind,'type_name':TYPES.get(kind,str(kind)),
                                  'sender_id':sender,'sender':'我' if me else self.names.get(sender,sender or self.names.get(user,user)),
                                  'is_me':me,'text':text,'raw':decode(content),'extra':extra})
                return list(reversed(items)) if newest else items
            finally:con.close()

    def media_batch(self, user, database, after=-1, limit=20, newest=False):
        return self.upload_batch(user, database, after, limit, platform=True, newest=newest, media_only=True)

    def spool_transcript(self,user,start=0,end=0,structured=False):
        """Merge all shards chronologically to a bounded-memory local file."""
        from datetime import datetime
        table='Msg_'+hashlib.md5(user.encode()).hexdigest()
        path=DATA/('analysis-'+uuid.uuid4().hex+'.txt')
        count=0;first=last=0;connections=[];digest=hashlib.sha256()
        with self.lock:
            try:
                streams=[]
                account=self.root.parent.name.rsplit('_',1)[0]
                for rel,tables in self.tables.items():
                    if table not in tables:continue
                    con=self.connect_db(rel);connections.append(con)
                    senders={r[0]:r[1] for r in con.execute('SELECT rowid,user_name FROM Name2Id')}
                    where='WHERE create_time >= ?'+(' AND create_time <= ?' if end else '')
                    args=[start]+([end] if end else [])
                    cursor=con.execute(f'SELECT * FROM [{table}] {where} ORDER BY create_time,local_id',args)
                    def rows(cursor=cursor,senders=senders,rel=rel):
                        for row in cursor:
                            yield row['create_time'],row['local_id'],rel,row,senders
                    streams.append(rows())
                with path.open('w',encoding='utf-8') as output:
                    for ts,local_id,rel,row,senders in heapq.merge(*streams,key=lambda x:x[:3]):
                        row=dict(row)
                        sender,text,extra=parse_content(row.get('message_content') or row.get('compress_content'),int(row['local_type'])&0xffffffff,'@chatroom' in user)
                        sender=senders.get(row['real_sender_id'],sender) or sender
                        name='我' if sender==account else self.names.get(sender,sender or self.names.get(user,user))
                        if structured:
                            item={'id':rel+':'+str(local_id),'user':user,'timestamp':ts,'sender':name,'sender_id':sender,'is_me':sender==account,'text':text}
                            line=json.dumps(item,ensure_ascii=False)+'\n'
                        else:line=f'[{datetime.fromtimestamp(ts).isoformat()}] {name}: {text}\n'
                        output.write(line);digest.update(line.encode('utf-8'))
                        count+=1;first=first or ts;last=ts
            except Exception:
                path.unlink(missing_ok=True);raise
            finally:
                for con in connections:con.close()
        return path,{'count':count,'first':first,'last':last,'digest':digest.hexdigest()}

wechat=Wechat()
