"""V2 Windows collector. Secrets use DPAPI; chat batches use AES-256-GCM."""
import base64
import hashlib
import json
import secrets
import threading
import time
import uuid

import httpx
from Crypto.Cipher import AES

from . import storage
from .remote_sync import source_key
from .remote_url import validate_server_url
from .sync_scope import is_message_database, is_sync_session


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def envelope(config, payload):
    batch_id = uuid.uuid4().hex
    key = base64.urlsafe_b64decode(config['encryption_key']+'='*(-len(config['encryption_key'])%4))
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(f"{config['app_key']}:{batch_id}".encode())
    encrypted, tag = cipher.encrypt_and_digest(json.dumps({**payload, 'sent_at': int(time.time())}, ensure_ascii=False, separators=(',', ':')).encode())
    return {'batch_id': batch_id, 'nonce': b64(nonce), 'ciphertext': b64(encrypted), 'tag': b64(tag)}


class PlatformUploader:
    def __init__(self):
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.states = {}
        self.readers = {}
        self.changed_messages = 0
        self.media_key_attempts = {}

    def records(self):
        return storage.read_json('platform-sync.bin', {}, secret=True)

    def config(self, w):
        return self.records().get(source_key(w), {}) if w.root else {}

    def allow_all(self, w):
        config = self.config(w)
        return bool(config.get('enabled') and config.get('scope') == 'all')

    def allowed(self, w, user):
        config = self.config(w)
        return bool(config.get('enabled') and (config.get('scope') == 'all' or w.in_workspace(user))
                    and w.can_sync_session(user))

    def settings(self, w):
        config = self.config(w)
        return {'url': config.get('url', ''), 'app_key': config.get('app_key', ''), 'has_secret': bool(config.get('app_secret')),
            'enabled': config.get('enabled', False), 'scope': config.get('scope', 'selected'),
            'status': self.states.get(source_key(w), {'state': 'idle', 'message': '尚未连接 2.0 平台', 'uploaded': 0, 'last_success': None})}

    def configure(self, w, url, app_key, app_secret, enabled, scope):
        key = source_key(w)
        if not key:
            raise ValueError('请先连接本机微信账号')
        if scope not in {'selected', 'all'}:
            raise ValueError('上传范围无效')
        url = validate_server_url(url) if url.strip() else ''
        with storage.WRITE_LOCK:
            records = self.records()
            previous = records.get(key, {})
            if not app_secret and url == previous.get('url') and app_key == previous.get('app_key'):
                app_secret = previous.get('app_secret', '')
            if enabled and (not url or not app_key.startswith('app_') or len(app_secret) < 32):
                raise ValueError('请填写服务端地址、appKey 和 appSecret')
            if any(not value.isascii() or any(c.isspace() for c in value) for value in (app_key, app_secret) if value):
                raise ValueError('客户端凭据格式无效')
            device = storage.read_json('platform-device.bin', {}, secret=True).get('id')
            if not device:
                device = uuid.uuid4().hex
                storage.write_json('platform-device.bin', {'id': device}, secret=True)
            config = {'url': url, 'app_key': app_key, 'app_secret': app_secret, 'enabled': enabled, 'scope': scope,
                'device_id': device, 'path': str(w.root), 'version': uuid.uuid4().hex}
            records[key] = config
            storage.write_json('platform-sync.bin', records, secret=True)
        self.states[key] = {'state': 'pending' if enabled else 'disabled', 'message': '等待注册与上传' if enabled else '已暂停上传，服务端数据保留', 'uploaded': 0, 'last_success': None}
        self.changed.set()
        return self.settings(w)

    def persist_token(self, key, config, registration):
        config.update(registration, expires_at=int(time.time())+registration['expires_in']-60)
        with storage.WRITE_LOCK:
            records = self.records()
            if records.get(key, {}).get('version') != config['version']:
                raise ValueError('客户端配置已改变，请重试')
            records[key] = config
            # One computer may authorize several WeChat accounts under the same appKey.
            # Keep their short-lived token synchronized so they do not revoke each other every round.
            for other in records.values():
                if (other.get('url'), other.get('app_key'), other.get('device_id'), other.get('app_secret')) == (
                        config['url'], config['app_key'], config['device_id'], config['app_secret']):
                    other.update(registration, expires_at=config['expires_at'])
            storage.write_json('platform-sync.bin', records, secret=True)

    @staticmethod
    def response(result):
        if result.status_code >= 300:
            messages = {401: '客户端凭据已失效或账号已停用', 403: '当前账号或客户端无此权限',
                409: '设备绑定或批次冲突，请检查是否使用了其他电脑的凭据', 422: '上传内容或凭据格式无效',
                429: '注册过于频繁，请稍后重试', 503: '服务端暂不可用，将自动重试'}
            raise ValueError(messages.get(result.status_code, f'服务端返回 HTTP {result.status_code}，将自动重试'))
        try:
            return result.json()
        except ValueError:
            raise ValueError('服务端响应格式无效') from None

    def register(self, client, key, config):
        response = client.post('api/v2/client/register', json={'app_key': config['app_key'], 'app_secret': config['app_secret'],
            'device_id': config['device_id'], 'version': '1.0.0'})
        data = self.response(response)
        self.persist_token(key, config, data)
        client.headers['Authorization'] = 'Bearer '+config['token']

    @staticmethod
    def client(config):
        headers = {'Authorization': 'Bearer '+config['token']} if config.get('token') else {}
        return httpx.Client(base_url=config['url']+'/', headers=headers,
            timeout=30, trust_env=False, follow_redirects=False)

    def connected(self, client, key, config):
        if config.get('expires_at', 0) < time.time():
            self.register(client, key, config)
        response = client.get('api/v2/client/status')
        if response.status_code == 401:
            self.register(client, key, config)
            response = client.get('api/v2/client/status')
        return self.response(response)

    def test(self, w):
        config = self.config(w)
        if not config.get('app_secret') or not config.get('url'):
            raise ValueError('请先保存平台凭据')
        try:
            with self.client(config) as client:
                self.connected(client, source_key(w), config)
        except httpx.HTTPError:
            raise ValueError('无法连接服务端') from None
        return {'ok': True, 'message': '客户端注册成功，已绑定当前电脑'}

    def priority_sessions(self, client, source):
        try:
            response = client.get('api/v2/client/priority-sessions', params={'source': source})
            if response.status_code == 404:  # Older servers keep the existing schedule.
                return None
            data = self.response(response)
            sessions = data.get('sessions')
            if not isinstance(sessions, list) or any(not isinstance(item, str) for item in sessions):
                raise ValueError('重点同步对象响应格式无效')
            return set(sessions)
        except (httpx.HTTPError, ValueError):
            # Do not broaden authorization or block ordinary collection when the
            # priority list cannot be obtained; retry it in the next round.
            return None

    def sync(self, w, full=False):
        if not self.lock.acquire(blocking=False):
            return {'state': 'busy'}
        started = time.monotonic()
        key, uploaded = source_key(w), 0
        sync = None
        try:
            config = self.config(w)
            if not config.get('enabled'):
                return {'state': 'disabled'}
            if w.status['state'] != 'ready':
                raise ValueError('正在等待本机微信解析完成')
            def guard():
                current = self.config(w)
                if source_key(w) != key or not current.get('enabled') or current.get('version') != config['version']:
                    raise ValueError('账号或上传授权已改变，同步已停止')
            previous = self.states.get(key, {})
            last = previous.get('last_success')
            self.states[key] = {'state': 'running', 'message': '正在检查重点同步对象', 'uploaded': 0,
                'last_success': last, 'priority_last_success': previous.get('priority_last_success'),
                'priority_count': 0, 'priority_available': None}
            account_name = w.root.parent.name
            common = {'source': key, 'account_name': account_name, 'self_id': account_name.rsplit('_',1)[0]}
            # Refresh and validate the contact authority before any network request.
            # Retry a previously observed database failure with a complete scan.
            with w.lock:
                if w.status.get('errors'):
                    w.sync()
                else:
                    w.sync(priority_users=set())
                if w.status.get('errors'):
                    raise ValueError('部分微信数据库读取失败，已暂停本轮上传并将重试')
                w.sync_contacts()
            with self.client(config) as client:
                capabilities = self.connected(client, key, config)
                from .incremental_sync import IncrementalSync
                sync = IncrementalSync(self, w, config, common, client, guard, full, started)
                priorities = self.priority_sessions(client, key)
                self.states[key]['priority_available'] = priorities is not None
                high_catalog = []
                catalog = []
                databases = []
                if priorities and not full:
                    self.states[key]['message'] = '正在优先检查重点会话'
                    with w.lock:
                        w.sync(priority_users=priorities)
                        if w.status.get('errors'):
                            raise ValueError('部分微信数据库读取失败，已暂停本轮上传并将重试')
                        selected = w.workspace()
                        contacts_map = w.sync_contacts()
                        allowed = {user for user in priorities if is_sync_session(user, contacts_map)
                                   and (config['scope'] == 'all' or user in selected)}
                        high_catalog = w.sessions_for(allowed)
                        high_catalog = [{k: s.get(k, default) for k, default in
                            [('id',''),('name',''),('note',''),('preview',''),('timestamp',0),('group',False)]} for s in high_catalog]
                        high_contacts = [contacts_map[user] for user in allowed if user in contacts_map]
                        high_databases = sorted(db for db in w.tables if is_message_database(db))
                    self.states[key].update(priority_count=len(high_catalog), message=f'正在优先同步 {len(high_catalog)} 个重点会话')
                    if high_catalog:
                        sync.manifest(high_catalog, high_contacts)
                        for session in high_catalog:
                            user = session['id']
                            if not sync.history_ready(user, high_databases) or user not in sync.progress.get('history_announced', {}):
                                sync.report_history(user, False)
                        sync.messages(high_catalog, high_databases, {s['id'] for s in high_catalog}, prune=False)
                        self.states[key].update(priority_last_success=int(time.time()), priority_uploaded=sync.uploaded,
                            uploaded=sync.uploaded, message='重点会话已优先检查，正在处理其他会话')
                high_ids = {s['id'] for s in high_catalog}
                normal_deadline = started + 9 if high_ids and not full else None
                if normal_deadline is not None and time.monotonic() >= normal_deadline:
                    sync.finish(audit_complete=False)
                    history_pending = True
                else:
                    # The normal pass keeps its existing scope and resumes later if the time slice expires.
                    with w.lock:
                        scan_complete = w.sync(deadline=normal_deadline) if normal_deadline is not None else w.sync()
                        if w.status.get('errors'):
                            raise ValueError('部分微信数据库读取失败，已暂停本轮上传并将重试')
                        selected = w.workspace()
                        sync_contacts = w.sync_contacts()
                        catalog = w.sessions() if config['scope'] == 'all' else w.workspace_sessions()
                        catalog = [s for s in catalog if is_sync_session(s['id'], sync_contacts)]
                        catalog = [{k: s.get(k, default) for k, default in [('id',''),('name',''),('note',''),('preview',''),('timestamp',0),('group',False)]} for s in catalog]
                        contacts = list(sync_contacts.values())
                        if config['scope'] != 'all':
                            contacts = [c for c in contacts if c['id'] in selected]
                        databases = sorted(database for database in w.tables if is_message_database(database))
                    manifest_complete = sync.manifest(catalog, contacts, deadline=normal_deadline)
                    if manifest_complete:
                        history_pending = sync.messages(catalog, databases, high_ids, prune=scan_complete is not False,
                            skip_priority=bool(high_ids), deadline=normal_deadline)
                    else:
                        history_pending = True
                    history_pending = history_pending or scan_complete is False
                    if scan_complete is not False:
                        for user in high_ids:
                            sync.report_history(user, sync.history_ready(user, databases, require_current=True))
                    sync.finish(audit_complete=manifest_complete and scan_complete is not False and sync.audit_complete(catalog, databases))
                if capabilities.get('media_protocol') == 1:
                    from .media_sync import MediaSync
                    media = MediaSync(sync)
                    media_catalog = {item['id']: item for item in high_catalog + catalog}
                    media_databases = sorted(set(databases) | set(high_databases if high_catalog else []))
                    media.run(list(media_catalog.values()), media_databases, high_ids)
                    self.states[key].update(media_uploaded=media.sent, media_unavailable=media.failed)
                uploaded = sync.uploaded
            message = f'增量同步完成 · 本轮上传 {uploaded} 条' + (' · 历史后台补齐中' if history_pending else ' · 已追平本机记录')
            if priorities is None:
                message += ' · 重点列表暂不可用'
            if capabilities.get('media_protocol') == 1:
                message += f" · 媒体上传 {self.states[key].get('media_uploaded', 0)} 项"
                if self.states[key].get('media_unavailable'):
                    message += ' · 部分媒体暂不可用，将重试'
            self.states[key].update(state='done', mode='incremental', history_pending=history_pending,
                uploaded=uploaded, message=message, last_success=int(time.time()))
            return self.states[key]
        except Exception as error:
            if sync is not None:
                uploaded = sync.uploaded
            message = str(error) if isinstance(error, ValueError) else '同步暂时失败，将自动重试'
            self.states[key] = {'state': 'error', 'message': message, 'uploaded': uploaded, 'last_success': self.states.get(key, {}).get('last_success')}
            return self.states[key]
        finally:
            self.lock.release()

    def loop(self, active, stop):
        from .wechat import Wechat
        while not stop.is_set():
            self.changed.clear()
            try:
                records = self.records()
                for key, config in records.items():
                    if stop.is_set():
                        break
                    if not config.get('enabled'):
                        self.readers.pop(key, None)
                        continue
                    if key == source_key(active):
                        reader = active
                    else:
                        reader = self.readers.get(key)
                        if reader is None:
                            reader = Wechat()
                            reader.connect(config['path'], persist=False)
                            self.readers[key] = reader
                    if reader.status['state'] not in {'ready', 'connecting'}:
                        reader.connect(config['path'], persist=False)
                    self.sync(reader)
            except Exception:
                pass  # Individual account states expose safe errors, never local credential data.
            for _ in range(30):
                if stop.wait(1) or self.changed.is_set():
                    break


platform_uploader = PlatformUploader()
