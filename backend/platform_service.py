"""Tenant-scoped business operations shared by REST, workers and MCP."""
import json
import secrets
import time
import uuid
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, delete, func, select, update
from sqlalchemy.exc import IntegrityError

from .platform_db import (accounts, audit, client_source, clients, contacts, history_sync, jobs, logical_sessions, message_pool, mcp_keys, memories, model_calls, model_stages,
    messages, models, receipts, row, rows, sessions, tokens, usage, users, versions)
from .platform_security import decode, digest, encode, password_hash, password_valid, secret, unseal
from .platform_models import SyncPayload


def now():
    return int(time.time())


def uid():
    return uuid.uuid4().hex


def mcp_allows(permission, write=False):
    return permission == 'full' or (not write and permission == 'read')


class NotFound(ValueError):
    pass


class Conflict(ValueError):
    pass


class Platform:
    def __init__(self, db, vault, documents, redis=None):
        self.db, self.vault, self.documents, self.redis = db, vault, documents, redis
        from .platform_workspace import Workspace
        from .platform_identity import Identity
        from .platform_media import MediaService
        self.workspace = Workspace(self)
        self.identity = Identity(self)
        self.media = MediaService(self)

    def log(self, con, user_id, action, target=''):
        con.execute(audit.insert().values(id=uid(), user_id=user_id, action=action, target=target, created_at=now()))

    def check_user(self, con, user_id, lock=False):
        user = row(con, users, users.c.id == user_id, lock=lock)
        if not user or not user['enabled']:
            raise PermissionError('用户已停用或不存在')
        return user

    def bootstrap(self, username, password):
        with self.db.transaction() as con:
            if con.execute(select(func.count()).select_from(users)).scalar_one():
                return
            con.execute(users.insert().values(id=uid(), username=username, name='平台管理员',
                password=password_hash(password), role='admin', enabled=True, must_change=True, epoch=1, created_at=now()))

    @staticmethod
    def public_user(user):
        return {k: user[k] for k in ('id', 'username', 'name', 'role', 'enabled', 'must_change', 'created_at')}

    def login(self, username, password):
        with self.db.transaction() as con:
            user = row(con, users, users.c.username == username.lower(), lock=True)
            if not user or not password_valid(password, user['password']) or not user['enabled']:
                raise PermissionError('登录名或密码不正确，或账号已停用')
            token = secret('web_')
            con.execute(tokens.insert().values(hash=digest(token), user_id=user['id'], kind='web', subject='',
                epoch=user['epoch'], expires=now()+43200))
            self.log(con, user['id'], 'login')
            return token, self.public_user(user)

    def authenticate(self, token, kind):
        if not token or len(token) > 200:
            raise PermissionError('请登录或提供有效凭据')
        with self.db.transaction() as con:
            if kind == 'mcp':
                key = row(con, mcp_keys, mcp_keys.c.hash == digest(token), mcp_keys.c.enabled == True)
                if not key or key['expires'] <= now():
                    raise PermissionError('MCP Key 已失效')
                user = self.check_user(con, key['user_id'])
                if user['must_change']:
                    raise PermissionError('请先修改初始密码')
                con.execute(mcp_keys.update().where(mcp_keys.c.id == key['id']).values(last_used=now()))
                return {'user_id': user['id'], 'role': user['role'], 'kind': kind, 'key': key}
            record = row(con, tokens, tokens.c.hash == digest(token), tokens.c.kind == kind, tokens.c.expires > now())
            if not record:
                raise PermissionError('凭据已过期，请重新登录或注册')
            user = self.check_user(con, record['user_id'])
            if user['epoch'] != record['epoch']:
                raise PermissionError('凭据已撤销')
            result = {'user_id': user['id'], 'role': user['role'], 'kind': kind, 'must_change': user['must_change'], 'user': self.public_user(user)}
            if kind == 'client':
                client = row(con, clients, clients.c.id == record['subject'], clients.c.enabled == True)
                if not client or user['must_change']:
                    raise PermissionError('客户端已停用或账号需修改密码')
                result['client'] = client
            return result

    def logout(self, token):
        with self.db.transaction() as con:
            con.execute(delete(tokens).where(tokens.c.hash == digest(token)))

    def change_password(self, user_id, old, new):
        with self.db.transaction() as con:
            user = self.check_user(con, user_id, True)
            if not password_valid(old, user['password']):
                raise PermissionError('当前密码不正确')
            con.execute(users.update().where(users.c.id == user_id).values(password=password_hash(new), must_change=False, epoch=user['epoch']+1))
            con.execute(delete(tokens).where(tokens.c.user_id == user_id))
            self.log(con, user_id, 'password_changed')

    def create_user(self, actor, body):
        with self.db.transaction() as con:
            self.require_admin(con, actor)
            user_id = uid()
            con.execute(users.insert().values(id=user_id, username=body.username.lower(), name=body.name,
                password=password_hash(body.password), role=body.role, enabled=True, must_change=True, epoch=1, created_at=now()))
            self.log(con, actor, 'user_created', user_id)
            return self.public_user(row(con, users, users.c.id == user_id))

    def require_admin(self, con, user_id):
        user = self.check_user(con, user_id)
        if user['role'] != 'admin':
            raise PermissionError('需要平台管理员权限')

    def update_user(self, actor, user_id, body):
        with self.db.transaction() as con:
            self.require_admin(con, actor)
            user = row(con, users, users.c.id == user_id, lock=True)
            if not user:
                raise NotFound('用户不存在')
            if user_id == actor and body.enabled is False:
                raise ValueError('不能停用当前登录管理员')
            changes = body.model_dump(exclude_none=True)
            if body.password:
                changes.update(password=password_hash(body.password), must_change=True)
            if body.password or body.enabled is False:
                changes['epoch'] = user['epoch']+1
                con.execute(delete(tokens).where(tokens.c.user_id == user_id))
            if changes:
                con.execute(users.update().where(users.c.id == user_id).values(**changes))
            if body.enabled is True:
                con.execute(jobs.update().where(jobs.c.user_id == user_id, jobs.c.state == 'paused').values(state='queued', next_run=0, updated_at=now()))
            self.log(con, actor, 'user_updated', user_id)
            return self.public_user(row(con, users, users.c.id == user_id))

    def create_client(self, user_id, name):
        client_id, app_key, app_secret = uid(), secret('app_')[:40], secret('secret_')
        with self.db.transaction() as con:
            self.check_user(con, user_id, True)
            con.execute(clients.insert().values(id=client_id, user_id=user_id, name=name,
                app_key=app_key, secret_hash=digest(app_secret), transport_key=self.vault.encrypt(user_id, 'transport', client_id, encode(secrets.token_bytes(32))),
                device_hash='', enabled=True, version='', last_seen=0, created_at=now()))
            self.log(con, user_id, 'client_created', client_id)
        return {'id': client_id, 'app_key': app_key, 'app_secret': app_secret}

    def client_action(self, actor, client_id, action, enabled=None, admin=False):
        with self.db.transaction() as con:
            if admin:
                self.require_admin(con, actor)
            item = row(con, clients, clients.c.id == client_id, lock=True)
            if not item or (not admin and item['user_id'] != actor):
                raise NotFound('客户端不存在')
            if admin and action != 'enabled':
                raise PermissionError('管理员仅可启停客户端')
            result = {'ok': True}
            changes = {}
            if action == 'rotate':
                value = secret('secret_')
                changes.update(secret_hash=digest(value), transport_key=self.vault.encrypt(actor, 'transport', client_id, encode(secrets.token_bytes(32))))
                result.update(app_key=item['app_key'], app_secret=value)
            elif action == 'unbind':
                changes['device_hash'] = ''
                value = secret('secret_')
                changes.update(secret_hash=digest(value), transport_key=self.vault.encrypt(actor, 'transport', client_id, encode(secrets.token_bytes(32))))
                result.update(app_key=item['app_key'], app_secret=value)
            elif action == 'enabled':
                changes['enabled'] = enabled
            else:
                raise ValueError('未知操作')
            con.execute(clients.update().where(clients.c.id == client_id).values(**changes))
            con.execute(delete(tokens).where(tokens.c.kind == 'client', tokens.c.subject == client_id))
            self.log(con, actor, 'client_'+action, client_id)
            return result

    def register(self, body):
        with self.db.transaction() as con:
            item = row(con, clients, clients.c.app_key == body.app_key, lock=True)
            if not item or not item['enabled'] or not secrets.compare_digest(item['secret_hash'], digest(body.app_secret)):
                raise PermissionError('客户端凭据无效或已停用')
            user = self.check_user(con, item['user_id'])
            if user['must_change']:
                raise PermissionError('请先登录平台修改初始密码')
            device = digest(body.device_id)
            if item['device_hash'] and item['device_hash'] != device:
                raise Conflict('此客户端已绑定另一台设备，请先在服务端解除绑定')
            token = secret('client_')
            con.execute(clients.update().where(clients.c.id == item['id']).values(device_hash=device, version=body.version, last_seen=now()))
            con.execute(delete(tokens).where(tokens.c.kind == 'client', tokens.c.subject == item['id']))
            con.execute(tokens.insert().values(hash=digest(token), user_id=user['id'], kind='client', subject=item['id'], epoch=user['epoch'], expires=now()+86400))
            self.log(con, user['id'], 'client_registered', item['id'])
            return {'token': token, 'expires_in': 86400, 'client_id': item['id'],
                'encryption_key': self.vault.decrypt(user['id'], 'transport', item['id'], item['transport_key'])}

    def decrypt_row(self, table_name, item):
        return self.vault.decrypt(item['user_id'], table_name, item['id'], item['payload'])

    @staticmethod
    def session_id(account_id, local_id):
        return digest(account_id+':'+local_id)

    def ingest(self, principal, envelope):
        user_id, client = principal['user_id'], principal['client']
        receipt_id = digest(client['id']+':'+envelope.batch_id)
        body_digest = digest(envelope.model_dump_json())
        key = decode(self.vault.decrypt(user_id, 'transport', client['id'], client['transport_key']))
        plaintext = unseal(key, envelope.model_dump(), f"{client['app_key']}:{envelope.batch_id}")
        data = SyncPayload.model_validate(plaintext)
        with self.db.transaction() as con:
            self.check_user(con, user_id, True)
            current = row(con, clients, clients.c.id == client['id'], clients.c.enabled == True, lock=True)
            if not current or current['secret_hash'] != client['secret_hash']:
                raise PermissionError('客户端授权已改变')
            receipt = row(con, receipts, receipts.c.id == receipt_id)
            if receipt:
                if receipt['digest'] != body_digest:
                    raise Conflict('批次标识已用于不同内容')
                return json.loads(receipt['result'])
            if abs(now()-data.sent_at) > 900:
                raise ValueError('上传已过期，请校准时间并重新发送')
            scoped_source = client_source(client['id'], data.source)
            account = row(con, accounts, accounts.c.user_id == user_id, accounts.c.source == scoped_source)
            if not account:
                if data.kind != 'manifest':
                    raise Conflict('请先同步微信账号和会话清单')
                account_id = uid()
                con.execute(accounts.insert().values(id=account_id, user_id=user_id, source=scoped_source, client_id=client['id'],
                    payload=self.vault.encrypt(user_id, 'accounts', account_id, {'name': data.account_name, 'self_id': data.self_id}), updated_at=now()))
            else:
                account_id = account['id']
                account_payload = self.decrypt_row('accounts', account)
                existing_identity = account_payload.get('self_id', '')
                if data.self_id and existing_identity and data.self_id != existing_identity:
                    raise Conflict('此数据源的微信账号已改变，请重新连接')
                values = dict(updated_at=now(), client_id=client['id'])
                if data.self_id and not existing_identity:
                    account_payload['self_id'] = data.self_id
                    values['payload'] = self.vault.encrypt(user_id, 'accounts', account_id, account_payload)
                con.execute(accounts.update().where(accounts.c.id == account_id).values(**values))
                if data.self_id and not existing_identity:
                    self.identity.reidentify(con, user_id, account_id, data.self_id)
            accepted = changed = 0
            if data.kind == 'manifest':
                for table_, kind, objects in ((contacts, 'contacts', data.contacts), (sessions, 'sessions', data.sessions)):
                    if len({o.id for o in objects}) != len(objects):
                        raise ValueError('清单含重复标识')
                    existing = {r['id']: r for r in rows(con, table_, table_.c.user_id == user_id,
                        table_.c.id.in_([self.session_id(account_id, obj.id) for obj in objects]), limit=500)}
                    for obj in objects:
                        object_id = self.session_id(account_id, obj.id)
                        old = existing.get(object_id)
                        if old and self.decrypt_row(kind, old) == obj.model_dump():
                            accepted += 1
                            continue
                        values = dict(payload=self.vault.encrypt(user_id, kind, object_id, obj.model_dump()), updated_at=now())
                        if old:
                            con.execute(table_.update().where(table_.c.id == object_id).values(**values))
                        else:
                            con.execute(table_.insert().values(id=object_id, user_id=user_id, account_id=account_id, **values))
                        accepted += 1
            elif data.kind == 'history':
                session_id = self.session_id(account_id, data.session)
                session = row(con, sessions, sessions.c.id == session_id, sessions.c.user_id == user_id)
                if not session:
                    raise Conflict('请先同步此会话清单')
                group_id, owner_id, siblings = self.identity.group(con, user_id, session, create=True)
                status = row(con, history_sync, history_sync.c.session_id == session_id, lock=True)
                values = {'complete': data.history_complete, 'updated_at': now()}
                if status:
                    con.execute(history_sync.update().where(history_sync.c.session_id == session_id).values(**values))
                else:
                    con.execute(history_sync.insert().values(session_id=session_id, user_id=user_id, **values))
                if data.history_complete:
                    if self.identity.history_complete(con, user_id, siblings):
                        con.execute(jobs.update().where(jobs.c.session_id == owner_id, jobs.c.user_id == user_id,
                            jobs.c.state == 'waiting_history').values(state='queued', error='', updated_at=now()))
                else:
                    con.execute(jobs.update().where(jobs.c.session_id == owner_id, jobs.c.user_id == user_id,
                        jobs.c.state.in_(['queued', 'running', 'waiting_model'])).values(state='waiting_history',
                        lease='', lease_until=0, error='等待客户端确认历史消息同步完成', updated_at=now()))
                accepted = 1
            else:
                session_id = self.session_id(account_id, data.session)
                session = row(con, sessions, sessions.c.id == session_id, sessions.c.user_id == user_id)
                if not session:
                    raise Conflict('请先同步此会话清单')
                group_id, owner_id, siblings = self.identity.group(con, user_id, session, create=True)
                owner = row(con, sessions, sessions.c.id == owner_id, sessions.c.user_id == user_id)
                if not data.items:
                    raise ValueError('消息批次不能为空')
                changed_times = set()
                for message in data.items:
                    if message.timestamp > now()+86400:
                        raise ValueError('消息时间无效')
                    server_id = str(message.server_id or '')
                    identity = 'server:'+server_id if server_id not in {'', '0'} else f"local:{client['id']}:{message.database}:{message.local_id}"
                    message_id = digest(session_id+':'+identity)
                    payload = message.model_dump()
                    fingerprint = digest(json.dumps(payload, sort_keys=True, ensure_ascii=False))
                    old = row(con, messages, messages.c.id == message_id)
                    if not old or old['fingerprint'] != fingerprint:
                        values = dict(client_id=client['id'], database_hash=digest(message.database), local_id=message.local_id,
                            timestamp=message.timestamp, fingerprint=fingerprint, payload=self.vault.encrypt(user_id, 'messages', message_id, payload), updated_at=now())
                        if old:
                            con.execute(messages.update().where(messages.c.id == message_id).values(**values))
                        else:
                            con.execute(messages.insert().values(id=message_id, user_id=user_id, session_id=session_id, **values))
                        selected = self.identity.add_message(con, user_id, group_id, message_id, payload)
                        if selected:
                            if old:
                                changed_times.add(old['timestamp'])
                            changed_times.add(message.timestamp)
                        changed += 1
                    accepted += 1
                if owner['listening'] and changed_times:
                    from .platform_jobs import enqueue_periods
                    enqueue_periods(con, user_id, owner, changed_times, debounce=True,
                        history_ready=self.identity.history_complete(con, user_id, siblings))
            result = {'accepted': accepted, 'changed': changed, 'account_id': account_id}
            con.execute(receipts.insert().values(id=receipt_id, client_id=client['id'], digest=body_digest, result=json.dumps(result), created_at=now()))
            con.execute(clients.update().where(clients.c.id == client['id']).values(last_seen=now()))
        self.wake()
        return result

    def wake(self):
        if self.redis:
            try:
                self.redis.publish('wechat:v2:jobs', 'wake')
            except Exception:
                pass  # SQL owns the queue; polling continues if Redis is unavailable.

    def cursors(self, principal, source, local_session):
        with self.db.transaction() as con:
            account = row(con, accounts, accounts.c.user_id == principal['user_id'],
                accounts.c.source == client_source(principal['client']['id'], source))
            if not account:
                return {}
            session_id = self.session_id(account['id'], local_session)
            result = con.execute(select(messages.c.database_hash, func.max(messages.c.local_id)).where(
                messages.c.user_id == principal['user_id'], messages.c.session_id == session_id,
                messages.c.client_id == principal['client']['id']).group_by(messages.c.database_hash))
            return {r[0]: r[1] for r in result}

    def priority_sessions(self, principal, source):
        """Share listening priorities across this user's matching WeChat identities."""
        with self.db.transaction() as con:
            account = row(con, accounts, accounts.c.user_id == principal['user_id'],
                accounts.c.source == client_source(principal['client']['id'], source))
            if not account:
                return []
            # Accounts and messages remain isolated by device. Only the priority
            # decision is shared, and only for an exact non-empty WeChat identity.
            identity = self.decrypt_row('accounts', account).get('self_id', '')
            account_ids = [account['id']]
            if identity:
                for candidate in con.execute(select(accounts).where(accounts.c.user_id == principal['user_id'],
                        accounts.c.id != account['id'])).mappings():
                    candidate = dict(candidate)
                    if self.decrypt_row('accounts', candidate).get('self_id') == identity:
                        account_ids.append(candidate['id'])
            result = con.execute(select(sessions).where(sessions.c.user_id == principal['user_id'],
                sessions.c.account_id.in_(account_ids), sessions.c.listening == True))
            return sorted({self.decrypt_row('sessions', dict(item))['id'] for item in result.mappings()})

    def session_access(self, con, principal, session_id, write=False):
        self.check_user(con, principal['user_id'])
        session = row(con, sessions, sessions.c.id == session_id, sessions.c.user_id == principal['user_id'])
        if not session:
            raise NotFound('会话不存在')
        _, owner_id, siblings = self.identity.group(con, principal['user_id'], session, create=False)
        owner = row(con, sessions, sessions.c.id == owner_id, sessions.c.user_id == principal['user_id'])
        if principal['kind'] == 'mcp':
            key = row(con, mcp_keys, mcp_keys.c.id == principal['key']['id'], mcp_keys.c.enabled == True)
            if not key or key['expires'] <= now() or not mcp_allows(key['permission'], write):
                raise PermissionError('MCP Key 无此读写权限或已失效')
            scope = json.loads(key['scopes'])
            if not owner['listening'] or (scope['sessions'] and not set(scope['sessions']).intersection(s['id'] for s in siblings)):
                raise PermissionError('会话不在此 MCP Key 的监听授权范围')
        _, owner_id, _ = self.identity.group(con, principal['user_id'], session, create=True)
        owner = row(con, sessions, sessions.c.id == owner_id, sessions.c.user_id == principal['user_id'])
        return owner

    def list_sessions(self, principal, account_id='', q='', offset=0, limit=50):
        with self.db.transaction() as con:
            user_id = principal['user_id']
            account_ids = set(self.identity.account_members(con, user_id, account_id)) if account_id else None
            scope = {}
            if principal['kind'] == 'mcp':
                if not mcp_allows(principal['key']['permission']):
                    raise PermissionError('需要只读或全部权限 MCP Key')
                scope = json.loads(principal['key']['scopes'])
            result = []
            groups = self.identity.catalog(con, user_id)
            for group in groups:
                if not group['sessions'] or (account_ids is not None and not account_ids.intersection(group['account_ids'])):
                    continue
                if principal['kind'] == 'mcp' and (not group['listening'] or (scope['sessions'] and not
                        set(scope['sessions']).intersection(r['id'] for r, _ in group['sessions']))):
                    continue
                if q and not any(q.casefold() in (p['name']+p['id']+p.get('note', '')).casefold() for _, p in group['sessions']):
                    continue
                item, payload = next((r,p) for r,p in group['sessions'] if r['id'] == group['id'])
                latest = max((p for _,p in group['sessions']), key=lambda p:p.get('timestamp', 0))
                payload = {**payload, 'timestamp': latest.get('timestamp', 0), 'preview': latest.get('preview', '')}
                result.append({**payload, 'source_id': payload['id'], 'id': item['id'], 'account_id': item['account_id'],
                    'listening': group['listening'], 'cycles': group['cycles'], 'updated_at': max(r['updated_at'] for r,_ in group['sessions'])})
            result.sort(key=lambda item: (item['updated_at'], item['id']), reverse=True)
            page = result[offset:offset+limit]
            page_groups = {}
            for item in page:
                source = row(con, sessions, sessions.c.id == item['id'], sessions.c.user_id == user_id)
                group_id, owner_id, _ = self.identity.group(con, user_id, source, create=True)
                page_groups[item['id']] = group_id
                item['id'] = owner_id
            counts = dict(con.execute(select(message_pool.c.group_id, func.count()).where(
                message_pool.c.user_id == user_id, message_pool.c.group_id.in_(list(page_groups.values())))
                .group_by(message_pool.c.group_id)).all())
            for item, group_id in zip(page, page_groups.values()):
                item['message_count'] = counts.get(group_id, 0)
            return {'items': page, 'total': len(result), 'has_more': offset+limit < len(result)}

    def list_catalog(self, user_id, kind, account_id='', q='', offset=0, limit=100):
        with self.db.transaction() as con:
            result = []
            if kind == 'accounts':
                for group in self.identity.account_groups(con, user_id):
                    item = min(group, key=lambda a: a['id'])
                    payload = self.decrypt_row('accounts', item)
                    if q.casefold() not in json.dumps(payload, ensure_ascii=False).casefold():
                        continue
                    result.append({**payload, 'source_id': payload.get('id'), 'id': item['id'],
                        'updated_at': max(a['updated_at'] for a in group), 'client_id': item['client_id'],
                        'source_count': len(group), 'client_count': len({a['client_id'] for a in group})})
            else:
                account_ids = set(self.identity.account_members(con, user_id, account_id)) if account_id else None
                for group in self.identity.catalog(con, user_id):
                    if not group['contacts'] or (account_ids is not None and not account_ids.intersection(group['account_ids'])):
                        continue
                    if q and not any(q.casefold() in json.dumps(p, ensure_ascii=False).casefold() for _,p in group['contacts']):
                        continue
                    payload = group['payload']
                    result.append({**payload, 'source_id': payload['id'], 'id': group['id'], 'account_id': group['account_id'],
                        'updated_at': max(r['updated_at'] for r,_ in group['contacts'])})
            return {'items': result[offset:offset+limit], 'total': len(result), 'has_more': offset+limit < len(result)}

    def read_messages(self, principal, session_id, offset=0, limit=100, q='', start=0, end=0, before='', after='', count=True):
        if not 0 <= offset <= 10000000 or not 1 <= limit <= 500 or start < 0 or end < 0 or (end and end < start):
            raise ValueError('分页或时间范围无效')
        with self.db.transaction() as con:
            session = self.session_access(con, principal, session_id)
            group_id, _, _ = self.identity.group(con, principal['user_id'], session, create=True)
            joined = messages.join(message_pool, message_pool.c.message_id == messages.c.id)
            conditions = [messages.c.user_id == principal['user_id'], message_pool.c.user_id == principal['user_id'],
                          message_pool.c.group_id == group_id, messages.c.timestamp >= start]
            if end:
                conditions.append(messages.c.timestamp < end)
            if before and after:
                raise ValueError('不能同时指定前后游标')
            if before or after:
                try:
                    stamp, identity = (before or after).split(':', 1)
                    stamp = int(stamp)
                    if len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ValueError('消息游标无效') from None
                conditions.append(or_(messages.c.timestamp > stamp, and_(messages.c.timestamp == stamp, messages.c.id > identity))
                    if after else or_(messages.c.timestamp < stamp, and_(messages.c.timestamp == stamp, messages.c.id < identity)))
            if not q:
                total = con.execute(select(func.count()).select_from(joined).where(*conditions)).scalar_one() if count else None
                order = (messages.c.timestamp, messages.c.id) if after else (messages.c.timestamp.desc(), messages.c.id.desc())
                page = list(con.execute(select(messages).select_from(joined).where(*conditions).order_by(*order).offset(offset).limit(limit+1)).mappings())
                has_more = len(page) > limit
                page = page[:limit]
                result = [{**self.decrypt_row('messages', r), 'evidence_id': r['id']} for r in page]
            else:
                # Stream the query, rather than loading an entire chat into RAM.
                total, result = 0, []
                for item in con.execute(select(messages).select_from(joined).where(*conditions).order_by(messages.c.timestamp.desc(), messages.c.id.desc()).execution_options(yield_per=200)).mappings():
                    payload = self.decrypt_row('messages', item)
                    if q.casefold() in (payload.get('text', '')+json.dumps(payload.get('extra', {}), ensure_ascii=False)).casefold():
                        if offset <= total < offset+limit:
                            result.append({**payload, 'evidence_id': item['id']})
                        total += 1
                has_more = offset+limit < total
            chronological = result if after else list(reversed(result))
            self.media.describe(con, principal['user_id'], group_id, chronological)
            def cursor(item):
                return f"{item['timestamp']}:{item['evidence_id']}" if item else ''
            return {'items': chronological, 'total': total, 'offset': offset, 'has_more': has_more,
                'older_cursor': cursor(chronological[0] if chronological else None),
                'newer_cursor': cursor(chronological[-1] if chronological else None),
                'next_offset': offset+limit if has_more else None}

    def get_message(self, principal, evidence_id):
        with self.db.transaction() as con:
            item = row(con, messages, messages.c.id == evidence_id, messages.c.user_id == principal['user_id'])
            if not item:
                raise NotFound('来源消息不存在')
            owner = self.session_access(con, principal, item['session_id'])
            result = {**self.decrypt_row('messages', item), 'evidence_id': item['id'], 'session_id': owner['id']}
            group_id, _, _ = self.identity.group(con, principal['user_id'], owner, create=True)
            self.media.describe(con, principal['user_id'], group_id, [result])
            return result

    def listen(self, principal, session_id, enabled, cycles):
        with self.db.transaction() as con:
            self.check_user(con, principal['user_id'], True)
            session = self.session_access(con, principal, session_id, write=True)
            group_id, _, siblings = self.identity.group(con, principal['user_id'], session, create=True)
            session_id = session['id']
            if session['listening'] == enabled and set(session['cycles'].split(',')) == set(cycles):
                return {'ok': True}
            con.execute(sessions.update().where(sessions.c.id == session_id).values(listening=enabled,
                listen_epoch=session['listen_epoch']+1, cycles=','.join(dict.fromkeys(cycles)), updated_at=now()))
            con.execute(jobs.update().where(jobs.c.session_id == session_id, jobs.c.user_id == principal['user_id']).values(state='cancelled', lease='', lease_until=0, updated_at=now()))
            if enabled:
                from .platform_jobs import enqueue_periods
                # One timestamp per Shanghai calendar day rather than every historical message.
                day = func.floor((messages.c.timestamp + 28800) / 86400) * 86400 - 28800
                timestamps = (int(value) for value in con.execute(select(day).select_from(messages.join(message_pool,
                    message_pool.c.message_id == messages.c.id)).where(message_pool.c.group_id == group_id,
                    message_pool.c.user_id == principal['user_id'], messages.c.user_id == principal['user_id']).distinct()).scalars())
                session.update(cycles=','.join(dict.fromkeys(cycles)))
                enqueue_periods(con, principal['user_id'], session, timestamps,
                    history_ready=self.identity.history_complete(con, principal['user_id'], siblings))
            self.log(con, principal['user_id'], 'listener_enabled' if enabled else 'listener_disabled', session_id)
        self.wake()
        return {'ok': True}

    def model_config(self, user_id, private=False):
        with self.db.transaction() as con:
            item = row(con, models, models.c.user_id == user_id)
        if not item:
            return {'configured': False, 'enabled': False}
        result = self.vault.decrypt(user_id, 'model', user_id, item['payload'])
        # Existing Qwen 3.8 configurations used expensive provider-default thinking.
        # Expose this conservative default; users can explicitly opt back in.
        if 'thinking_mode' not in result:
            host = urlsplit(result.get('base_url', '')).hostname or ''
            result['thinking_mode'] = ('disabled' if result.get('model', '').lower().startswith('qwen3.8')
                                       and (host == 'aliyuncs.com' or host.endswith('.aliyuncs.com')) else 'default')
        result.setdefault('thinking_budget', None)
        result.setdefault('daily_call_limit', 100)
        result.setdefault('analysis_max_tokens', 2048)
        result.setdefault('media_analysis', False)
        result.setdefault('audio_model', '')
        result.update(configured=True, revision=item['revision'])
        if not private:
            result['has_api_key'] = bool(result.pop('api_key', ''))
        return result

    def save_model(self, user_id, body):
        from .platform_jobs import validate_model_url
        validate_model_url(body.base_url)
        with self.db.transaction() as con:
            self.check_user(con, user_id, True)
            old = row(con, models, models.c.user_id == user_id, lock=True)
            config = body.model_dump()
            config['base_url'] = config['base_url'].rstrip('/')
            if not config['api_key'] and old:
                previous = self.vault.decrypt(user_id, 'model', user_id, old['payload'])
                if previous['base_url'] == config['base_url']:
                    config['api_key'] = previous.get('api_key', '')
            values = {'payload': self.vault.encrypt(user_id, 'model', user_id, config), 'revision': old['revision']+1 if old else 1, 'updated_at': now()}
            if old:
                con.execute(models.update().where(models.c.user_id == user_id).values(**values))
            else:
                con.execute(models.insert().values(user_id=user_id, **values))
            con.execute(jobs.update().where(jobs.c.user_id == user_id, jobs.c.state == 'waiting_model').values(state='queued', attempts=0, next_run=0, updated_at=now()))
            con.execute(jobs.update().where(jobs.c.user_id == user_id, jobs.c.state == 'queued',
                jobs.c.error == '已达到今日模型调用上限，将于明日自动继续').values(next_run=0, error='', updated_at=now()))
            self.log(con, user_id, 'model_configured')
        self.wake()
        return self.model_config(user_id)

    def create_mcp_key(self, user_id, body):
        key_id, value = uid(), secret('mcp_'+body.permission+'_')
        with self.db.transaction() as con:
            self.check_user(con, user_id, True)
            for session_id in body.sessions:
                item = row(con, sessions, sessions.c.id == session_id, sessions.c.user_id == user_id)
                if not item:
                    raise ValueError('授权会话不存在')
            con.execute(mcp_keys.insert().values(id=key_id, user_id=user_id, name=body.name, hash=digest(value), prefix=value[:16],
                permission=body.permission, scopes=json.dumps({'sessions': body.sessions, 'profile': body.profile}), enabled=True,
                expires=now()+86400*body.expires_days, last_used=0, created_at=now()))
            self.log(con, user_id, 'mcp_key_created', key_id)
        return {'id': key_id, 'key': value, 'permission': body.permission}

    def key_action(self, actor, key_id, action, enabled=None, admin=False):
        with self.db.transaction() as con:
            if admin:
                self.require_admin(con, actor)
            item = row(con, mcp_keys, mcp_keys.c.id == key_id, lock=True)
            if not item or (not admin and item['user_id'] != actor):
                raise NotFound('MCP Key 不存在')
            if action == 'rotate' and not admin:
                value = secret('mcp_'+item['permission']+'_')
                con.execute(mcp_keys.update().where(mcp_keys.c.id == key_id).values(hash=digest(value), prefix=value[:16]))
                result = {'key': value, 'id': key_id}
            elif action == 'revoke':
                con.execute(mcp_keys.update().where(mcp_keys.c.id == key_id).values(enabled=False, expires=now()))
                result = {'ok': True}
            elif action == 'enabled':
                if enabled and item['expires'] <= now():
                    raise Conflict('已撤销或过期的 Key 不能重新启用，请创建新 Key')
                con.execute(mcp_keys.update().where(mcp_keys.c.id == key_id).values(enabled=enabled))
                result = {'ok': True}
            else:
                raise PermissionError('无此操作权限')
            self.log(con, actor, 'mcp_key_'+action, key_id)
            return result

    def publish_memory(self, con, user_id, memory_id, session_id, kind, payload, start=0, end=0, expected=None, manual=False):
        current = row(con, memories, memories.c.id == memory_id, memories.c.user_id == user_id, lock=True)
        if expected is not None and (not current or current['version'] != expected):
            raise Conflict('记忆已有新版本，请刷新后再编辑')
        version = current['version']+1 if current else 1
        blob_id = uid()
        payload = {**payload, 'version': version, 'kind': kind, 'period_start': start, 'period_end': end,
            'generated_at': now(), 'manual': manual, 'previous_blob_id': current['blob_id'] if current else None}
        encrypted = self.vault.encrypt(user_id, 'memory', memory_id, payload)
        # Blob is immutable. SQL rollback leaves an unreferenced blob, never a broken published version.
        self.documents.put(blob_id, user_id, memory_id, encrypted)
        con.execute(versions.insert().values(blob_id=blob_id, memory_id=memory_id, user_id=user_id, version=version, manual=manual, created_at=now()))
        values = dict(version=version, blob_id=blob_id, manual=manual, updated_at=now())
        if current:
            # Auto output remains in history when a human revision is pinned as the current text.
            if current['manual'] and not manual:
                con.execute(memories.update().where(memories.c.id == memory_id).values(version=version, updated_at=now()))
            else:
                con.execute(memories.update().where(memories.c.id == memory_id).values(**values))
        else:
            con.execute(memories.insert().values(id=memory_id, user_id=user_id, session_id=session_id, kind=kind,
                period_start=start, period_end=end, **values))
        return {'id': memory_id, 'version': version}

    def memory_access(self, con, principal, memory_id, write=False):
        item = row(con, memories, memories.c.id == memory_id, memories.c.user_id == principal['user_id'])
        if not item:
            raise NotFound('记忆不存在')
        if item['session_id']:
            self.session_access(con, principal, item['session_id'], write)
        elif principal['kind'] == 'mcp':
            key = principal['key']
            if not mcp_allows(key['permission'], write) or not json.loads(key['scopes']).get('profile'):
                raise PermissionError('此 Key 未授权综合个人画像')
            # A composite profile needs unrestricted listening scope and all source sessions still enabled.
            if json.loads(key['scopes'])['sessions']:
                raise PermissionError('限定会话的 Key 不可读取跨会话综合画像')
            payload = self.vault.decrypt(item['user_id'], 'memory', item['id'], self.documents.get(item['blob_id'], item['user_id']))
            for source_id in payload.get('source_sessions', []):
                self.session_access(con, principal, source_id, write)
        return item

    def memory(self, principal, memory_id, version=0):
        with self.db.transaction() as con:
            item = self.memory_access(con, principal, memory_id)
            history = rows(con, versions, versions.c.memory_id == memory_id, versions.c.user_id == principal['user_id'], limit=100, order=versions.c.version.desc())
            blob_id = item['blob_id']
            if version:
                match = row(con, versions, versions.c.memory_id == memory_id, versions.c.user_id == principal['user_id'], versions.c.version == version)
                if not match:
                    raise NotFound('版本不存在')
                blob_id = match['blob_id']
            payload = self.vault.decrypt(principal['user_id'], 'memory', memory_id, self.documents.get(blob_id, principal['user_id']))
            return {**payload, 'id': memory_id, 'session_id': item['session_id'], 'latest_version': item['version'],
                'history': [{k: v[k] for k in ('version', 'manual', 'created_at')} for v in history]}

    def edit_memory(self, principal, memory_id, version, markdown):
        if not markdown.strip() or len(markdown) > 200000:
            raise ValueError('Markdown 内容不能为空且不能超过 200000 字符')
        with self.db.transaction() as con:
            self.check_user(con, principal['user_id'], True)
            item = self.memory_access(con, principal, memory_id, write=True)
            current = self.vault.decrypt(principal['user_id'], 'memory', memory_id, self.documents.get(item['blob_id'], principal['user_id']))
            result = self.publish_memory(con, principal['user_id'], memory_id, item['session_id'], item['kind'],
                {**current, 'markdown': markdown, 'author': 'mcp' if principal['kind'] == 'mcp' else 'user'},
                item['period_start'], item['period_end'], expected=version, manual=True)
            self.log(con, principal['user_id'], 'memory_edited', memory_id)
            return result

    def create_memory(self, principal, session_id, markdown, request_id):
        if not markdown.strip() or len(markdown) > 200000 or not 8 <= len(request_id) <= 100:
            raise ValueError('Markdown 或幂等请求标识无效')
        memory_id = digest(principal['user_id']+':manual:'+request_id)
        with self.db.transaction() as con:
            self.check_user(con, principal['user_id'], True)
            session_id = self.session_access(con, principal, session_id, write=True)['id']
            old = row(con, memories, memories.c.id == memory_id)
            if old:
                content = self.vault.decrypt(principal['user_id'], 'memory', memory_id, self.documents.get(old['blob_id'], principal['user_id']))
                if old['session_id'] != session_id or content['markdown'] != markdown:
                    raise Conflict('此请求标识已用于其他内容')
                return {'id': memory_id, 'version': old['version']}
            result = self.publish_memory(con, principal['user_id'], memory_id, session_id, 'manual',
                {'markdown': markdown, 'evidence': [], 'source_sessions': [session_id], 'author': 'mcp'}, manual=True)
            self.log(con, principal['user_id'], 'memory_created', memory_id)
            return result

    def list_memories(self, principal, session_id='', kind='', offset=0, limit=50):
        if principal['kind'] == 'mcp' and not mcp_allows(principal['key']['permission']):
            raise PermissionError('列出记忆需要只读或全部权限 MCP Key')
        with self.db.transaction() as con:
            conditions = [memories.c.user_id == principal['user_id']]
            if session_id:
                owner = self.session_access(con, principal, session_id)
                _, _, siblings = self.identity.group(con, principal['user_id'], owner, create=True)
                conditions.append(memories.c.session_id.in_([s['id'] for s in siblings]))
            if kind:
                conditions.append(memories.c.kind == kind)
            if principal['kind'] != 'mcp':
                total = con.execute(select(func.count()).select_from(memories).where(*conditions)).scalar_one()
                page = rows(con, memories, *conditions, limit=limit, offset=offset, order=memories.c.updated_at.desc())
                return {'items': [{k: item[k] for k in ('id','session_id','kind','period_start','period_end','version','manual','updated_at')}
                                  for item in page], 'total': total, 'has_more': offset+limit < total}
            result = []
            for item in rows(con, memories, *conditions, limit=50000, order=memories.c.updated_at.desc()):
                try:
                    self.memory_access(con, principal, item['id'])
                except PermissionError:
                    continue
                result.append({k: item[k] for k in ('id', 'session_id', 'kind', 'period_start', 'period_end', 'version', 'manual', 'updated_at')})
            return {'items': result[offset:offset+limit], 'total': len(result), 'has_more': offset+limit < len(result)}

    def overview(self, user_id, admin=False):
        with self.db.transaction() as con:
            if admin:
                self.require_admin(con, user_id)
            result = {}
            for name, table_ in [('users', users), ('clients', clients), ('accounts', accounts), ('contacts', contacts), ('sessions', sessions), ('messages', messages), ('memories', memories)]:
                query = select(func.count()).select_from(table_)
                if not admin:
                    query = query.where(table_.c.id == user_id) if table_ is users else query.where(table_.c.user_id == user_id)
                result[name] = con.execute(query).scalar_one()
            condition = [] if admin else [jobs.c.user_id == user_id]
            result['jobs'] = {r[0]: r[1] for r in con.execute(select(jobs.c.state, func.count()).where(*condition).group_by(jobs.c.state))}
            condition = [] if admin else [clients.c.user_id == user_id]
            result['online_clients'] = con.execute(select(func.count()).select_from(clients).where(*condition, clients.c.enabled == True, clients.c.last_seen > now()-90)).scalar_one()
            condition = [] if admin else [sessions.c.user_id == user_id]
            result['listening'] = con.execute(select(func.count()).select_from(sessions).where(*condition, sessions.c.listening == True)).scalar_one()
            if not admin:
                catalog = self.identity.catalog(con, user_id)
                result['accounts'] = len(self.identity.account_groups(con, user_id))
                result['contacts'] = sum(bool(group['contacts']) for group in catalog)
                result['sessions'] = sum(bool(group['sessions']) for group in catalog)
                result['listening'] = sum(group['listening'] for group in catalog)
            condition = [] if admin else [usage.c.user_id == user_id]
            totals = con.execute(select(func.sum(usage.c.input_tokens), func.sum(usage.c.output_tokens)).where(*condition)).one()
            result['tokens'] = {'input': totals[0] or 0, 'output': totals[1] or 0}
            result['checked_at'] = now()
            return result

    def list_operations(self, user_id, kind, admin=False, offset=0, limit=100):
        table_ = {'clients': clients, 'keys': mcp_keys, 'jobs': jobs, 'audit': audit, 'users': users}[kind]
        with self.db.transaction() as con:
            if admin:
                self.require_admin(con, user_id)
            elif kind == 'users':
                raise PermissionError('需要管理员权限')
            conditions = [] if admin else [table_.c.user_id == user_id]
            total = con.execute(select(func.count()).select_from(table_).where(*conditions)).scalar_one()
            order = table_.c.updated_at.desc() if kind == 'jobs' else table_.c.created_at.desc()
            result = rows(con, table_, *conditions, limit=limit, offset=offset, order=order)
            for item in result:
                for key in ('secret_hash', 'transport_key', 'device_hash', 'hash', 'password', 'lease'):
                    item.pop(key, None)
                if kind == 'clients':
                    item['online'] = item['enabled'] and item['last_seen'] > now()-90
                if kind == 'keys':
                    item['scopes'] = json.loads(item['scopes'])
            return {'items': result, 'total': total, 'has_more': offset+limit < total}

    def retry_job(self, user_id, job_id):
        with self.db.transaction() as con:
            item = row(con, jobs, jobs.c.id == job_id, jobs.c.user_id == user_id, lock=True)
            if not item:
                raise NotFound('任务不存在')
            session = row(con, sessions, sessions.c.id == item['session_id'], sessions.c.listening == True)
            if not session:
                raise Conflict('请先启用该会话的监听')
            if item['state'] == 'running' and item['lease_until'] > now():
                raise Conflict('任务正在运行')
            # A completed but invalid evaluation is retained encrypted for
            # diagnosis; explicit retry invalidates only that evaluation stage.
            invalid_stages = select(model_calls.c.stage_key).where(model_calls.c.user_id == user_id,
                model_calls.c.job_id == job_id, model_calls.c.state == 'failed', model_calls.c.error_code.like('memory_%'))
            con.execute(delete(model_stages).where(model_stages.c.user_id == user_id, model_stages.c.id.in_(invalid_stages)))
            con.execute(model_calls.update().where(model_calls.c.user_id == user_id, model_calls.c.job_id == job_id,
                model_calls.c.state.in_(['uncertain', 'inflight'])).values(state='acknowledged'))
            con.execute(model_calls.update().where(model_calls.c.user_id == user_id, model_calls.c.job_id == job_id,
                model_calls.c.state == 'success', model_calls.c.stage_key.not_in(
                    select(model_stages.c.id).where(model_stages.c.user_id == user_id))).values(state='acknowledged'))
            con.execute(jobs.update().where(jobs.c.id == job_id).values(state='queued', attempts=0, next_run=max(now(), item['period_end']+120), error='', lease='', updated_at=now()))
            self.log(con, user_id, 'job_retried', job_id)
        self.wake()
        return {'ok': True}
