"""SQLite persistence. Every operation is scoped to the authenticated account."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid


SCHEMA = '''
CREATE TABLE IF NOT EXISTS accounts (
 account TEXT PRIMARY KEY, source TEXT NOT NULL, revision TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 account TEXT NOT NULL, user TEXT NOT NULL, active INTEGER NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(account,user));
CREATE TABLE IF NOT EXISTS messages (
 account TEXT NOT NULL, user TEXT NOT NULL, id TEXT NOT NULL,
 database_name TEXT NOT NULL, local_id INTEGER NOT NULL, timestamp INTEGER NOT NULL,
 search_text TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(account,user,id), UNIQUE(account,user,database_name,local_id));
CREATE INDEX IF NOT EXISTS message_page ON messages(account,user,timestamp DESC,local_id DESC,id DESC);
CREATE INDEX IF NOT EXISTS message_cursor ON messages(account,user,database_name,local_id);
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        try:
            con.execute('PRAGMA journal_mode=WAL')
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()

    @contextmanager
    def connect(self, write=False):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            # Read pages and counts from one snapshot; serialize manifest and batch writes.
            con.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def status(self, account):
        with self.connect() as con:
            row = con.execute('SELECT * FROM accounts WHERE account=?', (account,)).fetchone()
            count = con.execute('SELECT COUNT(*) FROM sessions WHERE account=? AND active=1', (account,)).fetchone()[0]
            return {'state': 'ready' if row else 'idle', 'workspace_count': count,
                    'generation': row['generation'] if row else 0,
                    'updated_at': row['updated_at'] if row else None}

    def overview(self, account):
        with self.connect() as con:
            row = con.execute('SELECT generation,updated_at FROM accounts WHERE account=?', (account,)).fetchone()
            active = con.execute('SELECT COUNT(*) FROM sessions WHERE account=? AND active=1', (account,)).fetchone()[0]
            messages = con.execute('''SELECT COUNT(*),MIN(m.timestamp),MAX(m.timestamp) FROM messages m
                JOIN sessions s ON m.account=s.account AND m.user=s.user
                WHERE m.account=? AND s.active=1''', (account,)).fetchone()
        return {'account': account, 'state': 'ready' if row else 'idle', 'workspace_count': active,
                'message_count': messages[0], 'first_message': messages[1], 'last_message': messages[2],
                'generation': row['generation'] if row else 0, 'updated_at': row['updated_at'] if row else None,
                'checked_at': int(time.time())}

    def workspace(self, account, workspace):
        users = [s.id for s in workspace.sessions]
        if len(users) != len(set(users)):
            raise ValueError('会话 ID 重复')
        revision = uuid.uuid4().hex
        with self.connect(True) as con:
            old = con.execute('SELECT source FROM accounts WHERE account=?', (account,)).fetchone()
            if old and old['source'] != workspace.source:
                raise ValueError('上传凭据已绑定其他微信账号，请为新账号创建独立凭据')
            con.execute('''INSERT INTO accounts VALUES (?,?,?,1,?) ON CONFLICT(account) DO UPDATE SET
                revision=excluded.revision,generation=generation+1,updated_at=excluded.updated_at''',
                (account, workspace.source, revision, int(time.time())))
            con.execute('UPDATE sessions SET active=0 WHERE account=?', (account,))
            con.executemany('''INSERT INTO sessions VALUES (?,?,1,?) ON CONFLICT(account,user)
                DO UPDATE SET active=1,payload=excluded.payload''',
                [(account, s.id, s.model_dump_json()) for s in workspace.sessions])
        return {'revision': revision, 'sessions': len(users)}

    @staticmethod
    def require_session(con, account, user):
        row = con.execute('SELECT payload FROM sessions WHERE account=? AND user=? AND active=1', (account, user)).fetchone()
        if row is None:
            raise PermissionError('该会话未加入服务端工作台')
        return json.loads(row['payload'])

    def cursors(self, account, user):
        with self.connect() as con:
            self.require_session(con, account, user)
            return {r[0]: r[1] for r in con.execute('''SELECT database_name,MAX(local_id) FROM messages
                WHERE account=? AND user=? GROUP BY database_name''', (account, user))}

    def upload(self, account, batch):
        with self.connect(True) as con:
            row = con.execute('SELECT revision FROM accounts WHERE account=?', (account,)).fetchone()
            if not row or row['revision'] != batch.revision:
                raise ValueError('工作台版本已改变，请重新同步会话清单')
            self.require_session(con, account, batch.user)
            for m in batch.items:
                if m.id != f'{m.database}:{m.local_id}':
                    raise ValueError('消息 ID 与数据库及本地 ID 不一致')
                con.execute('''INSERT INTO messages VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(account,user,id)
                    DO UPDATE SET timestamp=excluded.timestamp,search_text=excluded.search_text,payload=excluded.payload''',
                    (account, batch.user, m.id, m.database, m.local_id, m.timestamp, m.text.casefold(), m.model_dump_json()))
            con.execute('UPDATE accounts SET generation=generation+1,updated_at=? WHERE account=?', (int(time.time()), account))
        return {'accepted': len(batch.items)}

    def sessions(self, account, q='', offset=0, limit=50):
        self.validate_page(offset, limit, 200)
        with self.connect() as con:
            items = [json.loads(r[0]) for r in con.execute('SELECT payload FROM sessions WHERE account=? AND active=1', (account,))]
        items = [s for s in items if q.casefold() in (s['name']+s['id']+s['note']).casefold()]
        items.sort(key=lambda s: (s['timestamp'], s['id']), reverse=True)
        public = [{k: v for k, v in s.items() if k != 'context'} for s in items[offset:offset+limit]]
        return {'items': public, 'total': len(items), 'offset': offset, 'has_more': offset+limit < len(items)}

    @staticmethod
    def validate_page(offset, limit, maximum=500, start=0, end=0):
        if offset < 0 or offset > 100000000 or not 1 <= limit <= maximum or start < 0 or end < 0 or (end and end < start):
            raise ValueError('分页或时间范围无效')

    def messages(self, account, user, offset=0, limit=100, q='', start=0, end=0, format='json'):
        self.validate_page(offset, limit, start=start, end=end)
        if format not in {'json', 'markdown'}:
            raise ValueError('仅支持 json / markdown')
        where = 'account=? AND user=? AND timestamp>=?'
        params = [account, user, start]
        if end:
            where += ' AND timestamp<=?'
            params.append(end)
        with self.connect() as con:
            session = self.require_session(con, account, user)
            if q:
                # Older records only indexed the message title. Search their
                # stored metadata as well without requiring a database rewrite.
                def metadata_search(payload):
                    try:
                        return json.dumps(json.loads(payload).get('extra', {}), ensure_ascii=False).casefold()
                    except (ValueError, TypeError, AttributeError):
                        return ''
                con.create_function('message_metadata_search', 1, metadata_search)
                where += ' AND (instr(search_text,?)>0 OR instr(message_metadata_search(payload),?)>0)'
                params.extend([q.casefold(), q.casefold()])
            total = con.execute('SELECT COUNT(*) FROM messages WHERE '+where, params).fetchone()[0]
            rows = con.execute('SELECT payload FROM messages WHERE '+where+' ORDER BY timestamp DESC,local_id DESC,id DESC LIMIT ? OFFSET ?', [*params, limit, offset]).fetchall()
            generation = con.execute('SELECT generation FROM accounts WHERE account=?', (account,)).fetchone()[0]
        result = {'items': [json.loads(r[0]) for r in reversed(rows)], 'total': total, 'offset': offset,
                  'has_more': offset+len(rows) < total, 'generation': generation,
                  'next_offset': offset+len(rows) if offset+len(rows) < total else None}
        if format == 'markdown':
            from helper.exporting import markdown
            result['markdown'] = markdown(result.pop('items'), user, session['name'])
        return result

    def context(self, account, user):
        with self.connect() as con:
            session = self.require_session(con, account, user)
            return {**session['context'], 'note': session['note']}
