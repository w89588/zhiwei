"""Encrypted, resumable media attached to authorized logical messages."""
import base64
import binascii
import hashlib
import math
import threading
from xml.etree import ElementTree as ET

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .platform_db import accounts, client_source, clients, media_chunks, media_objects, messages, row, sessions
from .platform_models import MediaPacket
from .platform_security import decode, digest, unseal
from .platform_service import Conflict, NotFound, now

CHUNK_SIZE = 512 * 1024


def quality(meta):
    name = meta.get('name', '')
    tier = 2 if name.startswith('image-original.') else 0 if name.startswith('image-thumbnail.') else 1
    return tier if meta.get('kind') == 3 else 1, meta.get('size', 0)


def media_type(kind, head):
    if kind in (3, 47):
        for magic, mime in ((b'\xff\xd8\xff', 'image/jpeg'), (b'\x89PNG\r\n\x1a\n', 'image/png'),
                            (b'GIF87a', 'image/gif'), (b'GIF89a', 'image/gif')):
            if head.startswith(magic):
                return mime
        if head.startswith(b'RIFF') and head[8:12] == b'WEBP':
            return 'image/webp'
        raise ValueError('图片格式无法识别')
    if kind == 43:
        if len(head) < 12 or head[4:8] != b'ftyp':
            raise ValueError('视频格式无法识别')
        return 'video/mp4'
    if kind == 34:
        if not (head.startswith(b'\x02#!SILK_V3') or head.startswith(b'#!SILK_V3')):
            raise ValueError('语音格式无法识别')
        return 'audio/silk'
    if kind == 49:
        return 'application/octet-stream'
    raise ValueError('此消息不支持媒体')


class MediaService:
    def __init__(self, platform):
        self.platform = platform
        self.emoji_locks = [threading.Lock() for _ in range(32)]

    def describe(self, con, user_id, group_id, items):
        keys = {self.platform.identity.message_key(item, item['evidence_id']): item
                for item in items if item.get('type') in (3, 34, 43, 47, 49)}
        if not keys:
            return
        rows = con.execute(select(media_objects).where(media_objects.c.user_id == user_id,
            media_objects.c.group_id == group_id, media_objects.c.message_key.in_(list(keys)),
            media_objects.c.state == 'ready').order_by(media_objects.c.updated_at.desc())).mappings()
        best = {}
        for item in rows:
            target = keys[item['message_key']]
            meta = self.platform.vault.decrypt(user_id, 'media', item['id'], item['payload'])
            if item['message_key'] not in best or quality(meta) > best[item['message_key']]:
                best[item['message_key']] = quality(meta)
                target['media'] = {k: meta[k] for k in ('name', 'mime', 'size', 'kind', 'sha256')}

    def ingest(self, principal, envelope):
        platform = self.platform
        user_id, client = principal['user_id'], principal['client']
        key = decode(platform.vault.decrypt(user_id, 'transport', client['id'], client['transport_key']))
        packet = MediaPacket.model_validate(unseal(key, envelope.model_dump(), f"{client['app_key']}:{envelope.batch_id}"))
        if abs(now() - packet.sent_at) > 900:
            raise ValueError('上传已过期，请校准时间并重新发送')
        if '/' in packet.name or '\\' in packet.name or packet.name in {'.', '..'} or any(ord(c) < 32 for c in packet.name):
            raise ValueError('文件名无效')
        count = math.ceil(packet.size / CHUNK_SIZE)
        with platform.db.transaction() as con:
            platform.check_user(con, user_id, True)
            current = row(con, clients, clients.c.id == client['id'], clients.c.enabled == True, lock=True)
            if not current or current['secret_hash'] != client['secret_hash']:
                raise PermissionError('客户端授权已改变')
            expired = list(con.execute(select(media_objects.c.id).where(media_objects.c.user_id == user_id,
                media_objects.c.state == 'uploading', media_objects.c.updated_at < now()-7*86400).limit(20)).scalars())
            if expired:
                con.execute(delete(media_chunks).where(media_chunks.c.object_id.in_(expired)))
                con.execute(delete(media_objects).where(media_objects.c.user_id == user_id, media_objects.c.id.in_(expired),
                    media_objects.c.state == 'uploading'))
            account = row(con, accounts, accounts.c.user_id == user_id,
                accounts.c.source == client_source(client['id'], packet.source))
            if not account:
                raise Conflict('请先同步微信账号')
            session_id = platform.session_id(account['id'], packet.session)
            session = row(con, sessions, sessions.c.id == session_id, sessions.c.user_id == user_id)
            if not session:
                raise Conflict('请先同步会话')
            group_id, owner_id, siblings = platform.identity.group(con, user_id, session, create=True)
            item = row(con, messages, messages.c.user_id == user_id, messages.c.session_id == session_id,
                messages.c.client_id == client['id'], messages.c.database_hash == digest(packet.database),
                messages.c.local_id == packet.local_id)
            if not item:
                raise Conflict('请先同步来源消息')
            source = platform.decrypt_row('messages', item)
            kind = source.get('type')
            if kind not in (3, 34, 43, 49):
                raise ValueError('来源消息不支持媒体')
            if kind == 49:
                try:
                    xml = ET.fromstring(source.get('raw', '').split(':\n', 1)[-1])
                    app = xml if xml.tag == 'appmsg' else xml.find('.//appmsg')
                    if app is None or app.findtext('type') != '6':
                        raise ValueError('来源消息不是文件附件')
                except ET.ParseError:
                    raise ValueError('文件消息格式无效') from None
            message_key = platform.identity.message_key(source, item['id'])
            blob_id = digest(':'.join((user_id, group_id, message_key, packet.sha256, packet.name)))
            meta = {'sha256': packet.sha256, 'size': packet.size, 'name': packet.name,
                    'mime': packet.mime, 'kind': kind, 'parts': count}
            current = row(con, media_objects, media_objects.c.id == blob_id, media_objects.c.user_id == user_id, lock=True)
            if current:
                if platform.vault.decrypt(user_id, 'media', blob_id, current['payload']) != meta:
                    raise Conflict('媒体元数据不一致')
                if current['state'] == 'ready':
                    return {'complete': True}
            else:
                con.execute(media_objects.insert().values(id=blob_id, user_id=user_id, group_id=group_id,
                    message_key=message_key, state='uploading', payload=platform.vault.encrypt(user_id, 'media', blob_id, meta),
                    created_at=now(), updated_at=now()))
            if packet.action == 'chunk':
                if packet.part >= count:
                    raise ValueError('媒体分片序号无效')
                try:
                    content = base64.b64decode(packet.data, validate=True)
                except (binascii.Error, ValueError):
                    raise ValueError('媒体分片编码无效') from None
                expected = min(CHUNK_SIZE, packet.size - packet.part * CHUNK_SIZE)
                if len(content) != expected:
                    raise ValueError('媒体分片长度无效')
                chunk_id = digest(f'{blob_id}:{packet.part}')
                previous = row(con, media_chunks, media_chunks.c.id == chunk_id)
                if previous:
                    if platform.vault.decrypt(user_id, 'media_chunk', chunk_id, previous['payload'])['data'] != packet.data:
                        raise Conflict('媒体分片内容不一致')
                else:
                    con.execute(media_chunks.insert().values(id=chunk_id, object_id=blob_id, part=packet.part,
                        payload=platform.vault.encrypt(user_id, 'media_chunk', chunk_id, {'data': packet.data})))
                con.execute(media_objects.update().where(media_objects.c.id == blob_id).values(updated_at=now()))
                return {'complete': False}
            if packet.data:
                raise ValueError('完成请求不能包含分片')
            chunks = list(con.execute(select(media_chunks).where(media_chunks.c.object_id == blob_id)
                .order_by(media_chunks.c.part)).mappings())
            if len(chunks) != count or [r['part'] for r in chunks] != list(range(count)):
                raise Conflict('媒体分片尚未全部上传')
            checksum, total, head = hashlib.sha256(), 0, b''
            for chunk in chunks:
                content = base64.b64decode(platform.vault.decrypt(user_id, 'media_chunk', chunk['id'], chunk['payload'])['data'], validate=True)
                if not head:
                    head = content[:32]
                checksum.update(content); total += len(content)
            if total != packet.size or checksum.hexdigest() != packet.sha256:
                raise ValueError('媒体完整性校验失败')
            detected = media_type(kind, head)
            # Browser-playable content must match its validated signature.
            if kind != 49 and detected != packet.mime:
                raise ValueError('媒体类型与内容不匹配')
            con.execute(media_objects.update().where(media_objects.c.id == blob_id).values(state='ready', updated_at=now()))
            owner = row(con, sessions, sessions.c.id == owner_id, sessions.c.user_id == user_id)
            if owner and owner['listening']:
                from .platform_jobs import enqueue_periods
                enqueue_periods(con, user_id, owner, [item['timestamp']], debounce=True,
                    history_ready=platform.identity.history_complete(con, user_id, siblings))
            return {'complete': True}

    def get(self, principal, evidence_id, offset=0, limit=None):
        try:
            return self._get(principal, evidence_id, offset, limit)
        except NotFound:
            try:
                cached = self._cache_emoji(principal, evidence_id)
            except IntegrityError:
                # Another server process may have stored the same emoji first.
                cached = True
            if cached:
                return self._get(principal, evidence_id, offset, limit)
            raise

    def _cache_emoji(self, principal, evidence_id):
        """Fetch once on demand and keep the result in the existing encrypted store."""
        platform, user_id = self.platform, principal['user_id']
        lock = self.emoji_locks[int(digest(evidence_id)[:8], 16) % len(self.emoji_locks)]
        with lock:
            try:
                self._get(principal, evidence_id, 0, 1)
                return True
            except NotFound:
                pass
            with platform.db.transaction() as con:
                item = row(con, messages, messages.c.id == evidence_id, messages.c.user_id == user_id)
                if not item:
                    return False
                session = platform.session_access(con, principal, item['session_id'])
                source = platform.decrypt_row('messages', item)
                if source.get('type') != 47:
                    return False
                raw = source.get('raw', '')
            from .emoji_media import fetch_emoji
            try:
                content, mime = fetch_emoji(raw)
            except ValueError:
                return False
            with platform.db.transaction() as con:
                # Permissions and source identity may have changed during network I/O.
                item = row(con, messages, messages.c.id == evidence_id, messages.c.user_id == user_id)
                if not item:
                    return False
                session = platform.session_access(con, principal, item['session_id'])
                current = platform.decrypt_row('messages', item)
                if current.get('type') != 47 or current.get('raw', '') != raw:
                    return False
                group_id, _, _ = platform.identity.group(con, user_id, session, create=True)
                message_key = platform.identity.message_key(current, item['id'])
                sha = hashlib.sha256(content).hexdigest()
                extension = {'image/gif':'gif', 'image/png':'png', 'image/jpeg':'jpg', 'image/webp':'webp'}[mime]
                name = 'emoji.' + extension
                blob_id = digest(':'.join((user_id, group_id, message_key, sha, name)))
                meta = {'sha256':sha, 'size':len(content), 'name':name, 'mime':mime,
                        'kind':47, 'parts':math.ceil(len(content)/CHUNK_SIZE)}
                if not row(con, media_objects, media_objects.c.id == blob_id):
                    con.execute(media_objects.insert().values(id=blob_id, user_id=user_id, group_id=group_id,
                        message_key=message_key, state='ready', payload=platform.vault.encrypt(user_id, 'media', blob_id, meta),
                        created_at=now(), updated_at=now()))
                    for part, start in enumerate(range(0, len(content), CHUNK_SIZE)):
                        chunk_id = digest(f'{blob_id}:{part}')
                        data = base64.b64encode(content[start:start+CHUNK_SIZE]).decode('ascii')
                        con.execute(media_chunks.insert().values(id=chunk_id, object_id=blob_id, part=part,
                            payload=platform.vault.encrypt(user_id, 'media_chunk', chunk_id, {'data':data})))
            return True

    def _get(self, principal, evidence_id, offset=0, limit=None):
        platform = self.platform
        user_id = principal['user_id']
        with platform.db.transaction() as con:
            item = row(con, messages, messages.c.id == evidence_id, messages.c.user_id == user_id)
            if not item:
                raise NotFound('来源消息不存在')
            session = platform.session_access(con, principal, item['session_id'])
            group_id, _, _ = platform.identity.group(con, user_id, session, create=True)
            message_key = platform.identity.message_key(platform.decrypt_row('messages', item), item['id'])
            objects = list(con.execute(select(media_objects).where(media_objects.c.user_id == user_id,
                media_objects.c.group_id == group_id, media_objects.c.message_key == message_key,
                media_objects.c.state == 'ready').order_by(media_objects.c.updated_at.desc())).mappings())
            if not objects:
                raise NotFound('媒体尚未从客户端同步')
            candidates = [(platform.vault.decrypt(user_id, 'media', obj['id'], obj['payload']), obj) for obj in objects]
            meta, obj = max(candidates, key=lambda pair:quality(pair[0]))
            if offset < 0 or offset > meta['size'] or (limit is not None and limit < 1):
                raise ValueError('媒体偏移超出范围')
            end = meta['size'] if limit is None else min(meta['size'], offset + limit)
            if end == offset:
                return meta, b''
            first, last = offset // CHUNK_SIZE, (end - 1) // CHUNK_SIZE
            chunks = list(con.execute(select(media_chunks).where(media_chunks.c.object_id == obj['id'],
                media_chunks.c.part >= first, media_chunks.c.part <= last).order_by(media_chunks.c.part)).mappings())
            if [r['part'] for r in chunks] != list(range(first, last + 1)):
                raise ValueError('媒体存储不完整')
            data = b''.join(base64.b64decode(platform.vault.decrypt(user_id, 'media_chunk', r['id'], r['payload'])['data'], validate=True) for r in chunks)
            data = data[offset - first * CHUNK_SIZE:offset - first * CHUNK_SIZE + end - offset]
            if len(data) != end - offset:
                raise ValueError('媒体存储不完整')
            if offset == 0 and end == meta['size'] and hashlib.sha256(data).hexdigest() != meta['sha256']:
                raise ValueError('媒体存储完整性校验失败')
            return meta, data
