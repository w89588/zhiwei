"""Lifecycle of the local collector; server credentials are independent of a WeChat account."""
import threading
import time
import uuid

import httpx

from . import storage
from .platform_sync import platform_uploader
from .remote_url import validate_server_url
from .wechat import discover, wechat


class Collector:
    def __init__(self):
        self.started = time.monotonic()
        self.changed = threading.Event()
        self.lock = threading.RLock()
        self.server_state = 'unconfigured'
        self.server_message = '请先连接服务端'
        self.parser_message = '等待服务端连接'
        self.verified_version = None

    def config(self):
        return storage.read_json('collector.bin', {}, secret=True)

    def verify(self, config):
        try:
            with platform_uploader.client(config) as client:
                registration = platform_uploader.response(client.post('api/v2/client/register', json={
                    'app_key': config['app_key'], 'app_secret': config['app_secret'],
                    'device_id': config['device_id'], 'version': '1.0.0'}))
                if not isinstance(registration, dict) or not isinstance(registration.get('token'), str) or not registration['token']:
                    raise ValueError('服务端注册响应格式无效')
                client.headers['Authorization'] = 'Bearer ' + registration['token']
                platform_uploader.response(client.get('api/v2/client/status'))
        except httpx.HTTPError:
            raise ValueError('无法连接服务端，请检查地址和网络') from None

    def configure(self, url, app_key, secret, consent):
        url = validate_server_url(url)
        app_key = app_key.strip()
        with self.lock:
            previous = self.config()
            if not secret and (url, app_key) == (previous.get('url'), previous.get('app_key')):
                secret = previous.get('app_secret', '')
            if not consent:
                raise ValueError('请先确认授权解析并上传所选微信账号')
            if not app_key.startswith('app_') or len(secret) < 32 or any(
                    not value.isascii() or any(c.isspace() for c in value) for value in (app_key, secret)):
                raise ValueError('请填写有效的 appKey 和 appSecret')
            device = storage.read_json('platform-device.bin', {}, secret=True).get('id') or uuid.uuid4().hex
            # Registration binds a device even if the following status check fails.
            # Reuse the same id on retries to avoid binding a new device each time.
            storage.write_json('platform-device.bin', {'id': device}, secret=True)
            config = {'url': url, 'app_key': app_key, 'app_secret': secret, 'device_id': device,
                      'account_path': previous.get('account_path', ''), 'version': uuid.uuid4().hex}
            self.verify(config)
            # Stop any current batch before changing its authorization.
            with platform_uploader.lock:
                storage.write_json('collector.bin', config, secret=True)
                self.verified_version = config['version']
                self.server_state, self.server_message = 'ready', '服务端连接正常'
            self.changed.set()
        return self.status()

    def select_account(self, path):
        with self.lock:
            config = self.config()
            if not config:
                raise ValueError('请先连接服务端')
            if path not in {item['path'] for item in discover()}:
                raise ValueError('未找到此微信账号，请刷新账号列表')
            with platform_uploader.lock:
                config['account_path'] = path
                config['version'] = uuid.uuid4().hex
                storage.write_json('collector.bin', config, secret=True)
                self.verified_version = config['version'] if self.server_state == 'ready' else None
            self.changed.set()
        return self.status()

    def status(self):
        config = self.config()
        accounts = discover() if config else []
        active = bool(config.get('account_path') and str(wechat.root or '') == config['account_path'])
        sync = platform_uploader.settings(wechat)['status'] if active else {}
        return {'configured': bool(config), 'url': config.get('url', ''), 'app_key': config.get('app_key', ''),
                'has_secret': bool(config.get('app_secret')), 'accounts': accounts,
                'account_path': config.get('account_path', ''), 'server_state': self.server_state,
                'server_message': self.server_message, 'parser_state': wechat.status['state'] if active else 'idle',
                'parser_message': self.parser_message if active or not config.get('account_path') else '等待加载所选账号', 'sync': sync,
                'changed_messages': platform_uploader.changed_messages,
                'uptime_seconds': int(time.monotonic() - self.started)}

    def loop(self, stop):
        configured_version = None
        while not stop.is_set():
            cycle_started = time.monotonic()
            priority_count = 0
            self.changed.clear()
            try:
                config = self.config()
                if not config:
                    self.server_state, self.server_message = 'unconfigured', '请先连接服务端'
                elif self.verified_version != config['version']:
                    self.server_state, self.server_message = 'connecting', '正在检查服务端连接'
                    self.verify(config)
                    if self.config().get('version') == config['version']:
                        self.verified_version = config['version']
                        self.server_state, self.server_message = 'ready', '服务端连接正常'
                if config and self.verified_version == config['version']:
                    accounts = discover()
                    paths = {item['path'] for item in accounts}
                    path = config.get('account_path', '')
                    if not path and len(accounts) == 1:
                        self.select_account(accounts[0]['path'])
                        continue
                    if path not in paths:
                        self.parser_message = '请选择微信账号' if accounts else '未发现微信账号，请登录微信后重试'
                    else:
                        if str(wechat.root or '') != path or wechat.status['state'] != 'ready':
                            self.parser_message = '正在加载微信解析器'
                            wechat.connect(path)
                        if wechat.status['state'] != 'ready':
                            self.parser_message = wechat.status['message']
                        else:
                            with self.lock:
                                if self.config().get('version') != config['version']:
                                    continue
                                if configured_version != config['version']:
                                    platform_uploader.configure(wechat, config['url'], config['app_key'], config['app_secret'], True, 'all')
                                    configured_version = config['version']
                                self.parser_message = wechat.status['message']
                                result = platform_uploader.sync(wechat)
                                priority_count = result.get('priority_count', 0)
                                if result.get('state') == 'error':
                                    self.server_state, self.server_message = 'error', result['message']
                                else:
                                    self.server_state, self.server_message = 'ready', '服务端连接正常'
                                self.parser_message = wechat.status['message']
            except Exception as error:
                self.server_state = 'error'
                self.server_message = str(error) if isinstance(error, ValueError) else '连接或解析暂时失败，将自动重试'
                self.verified_version = None
            wait = max(0, 10 - (time.monotonic() - cycle_started)) if priority_count else 10
            until = time.monotonic() + wait
            while not stop.is_set() and not self.changed.is_set():
                remaining = until - time.monotonic()
                if remaining <= 0 or stop.wait(min(1, remaining)):
                    break


collector = Collector()
