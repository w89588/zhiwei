"""Windows-side upload worker. Database keys and source paths never leave this process."""
import hashlib
import json
import threading
import time
import uuid

import httpx

from . import storage
from .remote_url import validate_server_url as validate_url


def source_key(w):
    from .memory_store import account_key
    return account_key(w.root) if w.root else None


class Uploader:
    def __init__(self):
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.states = {}
        self.completed = {}

    def config(self, w):
        key = source_key(w)
        return storage.read_json('remote-sync.bin', {}, secret=True).get(key, {}) if key else {}

    def settings(self, w):
        config = self.config(w)
        return {'enabled': config.get('enabled', False), 'url': config.get('url', ''),
                'has_token': bool(config.get('token')), 'status': self.states.get(source_key(w),
                {'state': 'idle', 'message': '尚未上传', 'uploaded': 0, 'last_success': None})}

    def configure(self, w, url, token, enabled):
        key = source_key(w)
        if key is None:
            raise ValueError('请先连接微信账号，再配置此账号的服务端')
        with storage.WRITE_LOCK:
            records = storage.read_json('remote-sync.bin', {}, secret=True)
            old = records.get(key, {})
            url = validate_url(url) if url.strip() else ''
            token = token.strip()
            if not token and url == old.get('url'):
                token = old.get('token', '')
            if enabled and (not url or len(token) < 32):
                raise ValueError('启用上传需要服务端地址及至少 32 位的上传 token；更换地址后请重新填写 token')
            if token and (not token.isascii() or any(c.isspace() for c in token)):
                raise ValueError('上传 token 格式无效')
            records[key] = {'url': url, 'token': token, 'enabled': enabled, 'version': uuid.uuid4().hex}
            storage.write_json('remote-sync.bin', records, secret=True)
        self.completed.pop(key, None)
        self.changed.set()
        self.states[key] = {'state': 'pending' if enabled else 'disabled',
                            'message': '等待上传' if enabled else '自动上传已关闭；服务端已有数据仍保留',
                            'uploaded': 0, 'last_success': None}
        return self.settings(w)

    @staticmethod
    def request(client, method, path, **kwargs):
        try:
            response = client.request(method, path, **kwargs)
        except httpx.RequestError:
            raise ValueError('无法连接服务端，将在下一轮自动重试') from None
        if response.status_code == 401:
            raise ValueError('上传凭据无效或权限不足，请检查服务端配置')
        if response.status_code == 409:
            raise ValueError('账号绑定或同步版本冲突，请确认上传凭据属于当前账号后重试')
        if response.is_error:
            raise ValueError(f'服务端拒绝上传（HTTP {response.status_code}），请检查配置或消息大小')
        try:
            return response.json()
        except ValueError:
            raise ValueError('服务端返回格式无效') from None

    @staticmethod
    def client(config):
        return httpx.Client(base_url=config['url']+'/', headers={'Authorization': 'Bearer '+config['token']},
                            timeout=30, trust_env=False, follow_redirects=False)

    def test(self, w):
        config = self.config(w)
        if not config.get('url') or not config.get('token'):
            raise ValueError('请先保存服务端地址和上传 token')
        with self.client(config) as client:
            result = self.request(client, 'GET', 'api/v1/client/status')
        if 'state' not in result or 'workspace_count' not in result:
            raise ValueError('该地址不是兼容的微信对话服务端')
        return {'ok': True, 'message': '连接成功，上传权限有效'}

    def sync(self, w, full=False, force=False):
        if not self.lock.acquire(blocking=False):
            return {'state': 'busy', 'message': '正在上传，请稍候'}
        key = None
        uploaded = 0
        try:
            from . import chat_memory, knowledge, memory_store
            with w.lock:
                key = source_key(w)
                config = self.config(w)
                if not config.get('enabled'):
                    return {'state': 'disabled'}
                if w.status['state'] != 'ready':
                    raise ValueError('客户端尚未完成微信解析')
                sessions = []
                profiles = chat_memory.read(w, 'profiles')
                summaries = chat_memory.read(w, 'summaries')
                for session in w.workspace_sessions():
                    user = session['id']
                    old = summaries.get(user, [])
                    context = {'profile': profiles.get(user, {}), 'latest_summary': old[-1] if old else None,
                               'knowledge': knowledge.confirmed_context(w, user),
                               'period_summaries': memory_store.journals(key, set(w.workspace()), user, limit=12)}
                    sessions.append({k: session.get(k, default) for k, default in
                                     [('id', ''), ('name', ''), ('note', ''), ('preview', ''), ('timestamp', 0), ('group', False)]} | {'context': context})
                workspace = {'source': key, 'sessions': sessions}
                selection = w.workspace()
                generation = w.status['generation']
                databases = sorted(w.tables)
            signature = hashlib.sha256(json.dumps([config['version'], generation, workspace], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            def guard():
                # Check between network requests, allowing account changes and disabling to cancel.
                with w.lock:
                    current = self.config(w)
                    if (source_key(w) != key or not current.get('enabled') or current.get('version') != config['version']
                            or w.status['state'] != 'ready' or w.workspace() != selection):
                        raise ValueError('账号、会话或解析数据已变化，下一轮重新同步')

            previous = self.states.get(key, {}).get('last_success')
            self.states[key] = {'state': 'running', 'message': '正在同步会话清单', 'uploaded': 0, 'last_success': previous}
            with self.client(config) as client:
                guard()
                status = self.request(client, 'GET', 'api/v1/client/status')
                if not force and not full and self.completed.get(key) == (signature, status['generation']) and status['state'] == 'ready':
                    self.states[key].update(state='done', message='已同步，暂无新记录')
                    return self.states[key]
                revision = self.request(client, 'POST', 'api/v1/client/workspace', json=workspace)['revision']
                for session in sessions:
                    user = session['id']
                    guard()
                    cursors = {} if full else self.request(client, 'GET', 'api/v1/client/cursors', params={'user': user})['cursors']
                    for database in databases:
                        # Re-read a small overlap to propagate recent edits/revocations and delayed records.
                        after = max(-1, cursors.get(database, -1)-100)
                        while True:
                            guard()
                            items = w.upload_batch(user, database, after, 200)
                            if not items:
                                break
                            # Bound bytes as well as count; split large text records into separate requests.
                            batches, batch, size = [], [], 0
                            for item in items:
                                item_size = len(json.dumps(item, ensure_ascii=False).encode())
                                if batch and size+item_size > 4*1024*1024:
                                    batches.append(batch); batch, size = [], 0
                                batch.append(item); size += item_size
                            if batch:
                                batches.append(batch)
                            for batch in batches:
                                guard()
                                self.request(client, 'POST', 'api/v1/client/messages', json={'revision': revision, 'user': user, 'items': batch})
                                uploaded += len(batch)
                                self.states[key].update(message=f'正在上传 {session["name"]} · 本轮已处理 {uploaded} 条', uploaded=uploaded)
                            after = items[-1]['local_id']
                guard()
                remote_generation = self.request(client, 'GET', 'api/v1/client/status')['generation']
            self.completed[key] = (signature, remote_generation)
            self.states[key].update(state='done', message=f'同步完成 · 本轮处理 {uploaded} 条（重试与重叠记录自动去重）', last_success=int(time.time()))
            return self.states[key]
        except Exception as error:
            # Never include request/response bodies, local paths or credentials in status/logs.
            message = str(error) if isinstance(error, ValueError) else '上传失败，将在下一轮自动重试'
            self.states[key] = {'state': 'error', 'message': message, 'uploaded': uploaded,
                                'last_success': self.states.get(key, {}).get('last_success')}
            return self.states[key]
        finally:
            self.lock.release()

    def loop(self, w, stop):
        while not stop.is_set():
            self.changed.clear()
            self.sync(w)
            # Wake on settings/selection changes or retry after 15 seconds.
            for _ in range(15):
                if stop.wait(1) or self.changed.is_set():
                    break


uploader = Uploader()
