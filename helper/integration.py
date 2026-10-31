"""Opt-in, per-user discovery for the read-only MCP bridge."""
import secrets
import sys
import threading
from pathlib import Path
from .storage import DATA, read_json, write_json

SKILL = Path(__file__).resolve().parent.parent / 'skills' / 'wechat-chat' / 'SKILL.md'


class Integration:
    def __init__(self):
        self.lock = threading.RLock()
        self.enabled = bool(read_json('integration.json', {}).get('enabled', False))
        self.token = secrets.token_urlsafe(32)
        self.url = ''

    def publish(self, url):
        with self.lock:
            self.url = url
            self._write_connection()

    def _write_connection(self):
        if self.enabled and self.url:
            write_json('integration-connection.bin', {'url': self.url, 'token': self.token}, secret=True)

    def configure(self, enabled):
        with self.lock:
            self.cleanup()
            self.enabled = enabled
            self.token = secrets.token_urlsafe(32)
            write_json('integration.json', {'enabled': enabled})
            if enabled:
                self._write_connection()
            else:
                self.cleanup()

    def cleanup(self):
        with self.lock:
            path = DATA / 'integration-connection.bin'
            # Do not remove another running instance's discovery file.
            if path.exists():
                try:
                    if read_json(path.name, {}, secret=True).get('token') == self.token:
                        path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass

    def settings(self):
        if getattr(sys, 'frozen', False):
            command = str(Path(sys.executable).parent / 'WechatHelperMCP.exe')
            args = []
        else:
            command = sys.executable
            args = [str(Path(__file__).resolve().parent.parent / 'mcp_main.py')]
        server = {'command': command, 'args': args,
                  'env': {'WECHAT_HELPER_DATA': str(DATA.resolve())}}
        return {'enabled': self.enabled, 'config': {'mcpServers': {'wechat-helper': server}}}


integration = Integration()
