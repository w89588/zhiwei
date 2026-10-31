"""Recent-first media upload with an independent durable history cursor."""
import base64
import hashlib
import time
from xml.etree import ElementTree as ET

from .media import find_image_key, media_for
from .incremental_sync import fingerprint
from .platform_sync import envelope

CHUNK_SIZE = 512 * 1024


def is_attachment(message):
    if message['type'] in (3, 34, 43):
        return True
    if message['type'] != 49:
        return False
    try:
        root = ET.fromstring(message['raw'].split(':\n', 1)[-1])
        app = root if root.tag == 'appmsg' else root.find('.//appmsg')
        return app is not None and app.findtext('type') == '6'
    except ET.ParseError:
        return False


class MediaSync:
    def __init__(self, sync):
        self.sync = sync
        self.remaining = 5
        self.sent = 0
        self.failed = 0

    def post(self, packet):
        self.sync.guard()
        response = self.sync.client.post('api/v2/client/media', json=envelope(self.sync.config,
            {'source':self.sync.common['source'], **packet}))
        return self.sync.uploader.response(response)

    def transfer(self, user, database, message):
        self.sync.guard()
        if not self.sync.uploader.allowed(self.sync.w, user):
            raise ValueError('会话上传范围已改变')
        try:
            data, mime, name = media_for(self.sync.w, user, database, message)
        except ValueError as error:
            if 'V2 图片密钥' not in str(error):
                raise
            # Key extraction can be expensive. Try at most once per account
            # per five minutes, and leave the message pending when unavailable.
            key = self.sync.common['source']
            now = time.time()
            attempts = self.sync.uploader.media_key_attempts
            if now - attempts.get(key, 0) < 300:
                raise
            attempts[key] = now
            find_image_key(self.sync.w)
            data, mime, name = media_for(self.sync.w, user, database, message)
        sha = hashlib.sha256(data).hexdigest()
        packet = {'session':user, 'database':database, 'local_id':message['local_id'],
                  'sha256':sha, 'size':len(data), 'name':name[:255], 'mime':mime}
        for part, offset in enumerate(range(0, len(data), CHUNK_SIZE)):
            result = self.post({**packet, 'action':'chunk', 'part':part,
                'data':base64.b64encode(data[offset:offset+CHUNK_SIZE]).decode('ascii')})
            if result.get('complete'):
                return
        if not self.post({**packet, 'action':'commit'}).get('complete'):
            raise ValueError('服务端未确认媒体上传完成')

    def process_item(self, user, database, message, state):
        if not is_attachment(message):
            return True
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        pending = state.setdefault('media_pending', {})
        key = str(message['local_id'])
        try:
            self.transfer(user, database, message)
            pending.pop(key, None)
            self.sent += 1
        except Exception as error:
            reason = str(error)[:120] if isinstance(error, ValueError) else '本机媒体暂不可用，将重试'
            pending[key] = {'retry_at':int(time.time()) + 300, 'reason':reason}
            self.failed += 1
        self.sync.save()
        return True

    def run(self, catalog, databases, priorities):
        sessions = sorted(catalog, key=lambda s: (s['id'] not in priorities, s['id']))
        shards = [(s['id'], db) for s in sessions for db in databases
                  if 'Msg_'+hashlib.md5(s['id'].encode()).hexdigest() in self.sync.w.tables.get(db, set())]
        if shards:
            turn = self.sync.progress.get('media_turn', 0) % len(shards)
            shards = shards[turn:] + shards[:turn]
            self.sync.progress['media_turn'] = turn + 1
            self.sync.save()
        for user, database in shards:
            if not self.sync.uploader.allowed(self.sync.w, user):
                continue
            state = self.sync.progress['shards'].setdefault(fingerprint([user, database]), {})
            # First pass sends recent media, then older history is traversed
            # from the beginning with a separate cursor.
            if 'media_tail' not in state:
                page = self.sync.w.media_batch(user, database, -1, 20, newest=True)
                state['media_tail'] = page[-1]['local_id'] if page else -1
                state['media_before'] = page[0]['local_id'] if page else 0
                state['media_history'] = -1
                state['media_recent'] = [m['local_id'] for m in reversed(page)]
                self.sync.save()
            while state.get('media_recent'):
                local_id = state['media_recent'][0]
                matches = self.sync.w.media_batch(user, database, local_id-1, 1)
                if matches and matches[0]['local_id'] == local_id:
                    if not self.process_item(user, database, matches[0], state):
                        self.sync.save(); return
                state['media_recent'].pop(0); self.sync.save()
            page = self.sync.w.media_batch(user, database, state['media_tail'], 20)
            for message in page:
                if not self.process_item(user, database, message, state):
                    self.sync.save(); return
                state['media_tail'] = message['local_id']
                self.sync.save()
            for local_id, info in list(state.get('media_pending', {}).items()):
                if info.get('retry_at', 0) > time.time():
                    continue
                if self.remaining <= 0:
                    self.sync.save(); return
                candidates = self.sync.w.media_batch(user, database, int(local_id)-1, 1)
                if not candidates or candidates[0]['local_id'] != int(local_id):
                    state['media_pending'].pop(local_id, None); self.sync.save(); continue
                self.process_item(user, database, candidates[0], state)
            if state['media_before'] and state['media_history'] < state['media_before'] - 1:
                older = self.sync.w.media_batch(user, database, state['media_history'], 20)
                older = [m for m in older if m['local_id'] < state['media_before']]
                for message in older:
                    if not self.process_item(user, database, message, state):
                        self.sync.save(); return
                    state['media_history'] = message['local_id']; self.sync.save()
                if not older:
                    state['media_history'] = state['media_before']; self.sync.save()
            if self.remaining <= 0:
                return
