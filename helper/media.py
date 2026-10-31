"""Local media resolution. Never downloads attachments or accepts arbitrary paths."""
import ctypes
import ctypes.wintypes as wt
import hashlib
import mimetypes
import re
import sqlite3
import struct
import subprocess
import sys
import os
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import psutil
from .wechat import wechat
from .storage import read_json,write_json

MAGIC={b'\xff\xd8\xff':'image/jpeg',b'\x89PNG':'image/png',b'GIF8':'image/gif',b'RIFF':'image/webp'}

def mime(data):
    for magic,kind in MAGIC.items():
        if data.startswith(magic):
            if kind == 'image/webp' and (len(data) < 12 or data[8:12] != b'WEBP'):
                continue
            return kind
    return None

def decode_dat(data,key=None,xor_key=None):
    if not data:raise ValueError('图片文件为空')
    if data[:6] in (b'\x07\x08V1\x08\x07',b'\x07\x08V2\x08\x07'):
        if len(data)<31:raise ValueError('图片文件不完整')
        if data[3:4]==b'1':key=b'cfcd208495d565ef'
        if not key:raise ValueError('V2 图片密钥尚未提取')
        size,tail=struct.unpack_from('<II',data,6);aligned=(size//16+1)*16
        if 15+aligned>len(data) or tail>len(data)-15-aligned:raise ValueError('图片结构无效')
        head=unpad(AES.new(key,AES.MODE_ECB).decrypt(data[15:15+aligned]),16)
        raw=data[15+aligned:len(data)-tail] if tail else data[15+aligned:]
        end=data[-tail:] if tail else b''
        xor=0x88 if xor_key is None else xor_key
        if xor_key is None and len(end)>=2 and end[-2]^0xff==end[-1]^0xd9:xor=end[-2]^0xff
        output=head+raw+bytes(v^xor for v in end)
        if output.startswith(b'wxgf'):
            return decode_wxgf(output), 'image/jpeg'
        if not mime(output):raise ValueError('图片是微信专用编码，当前仅能展示元数据')
        return output,mime(output)
    for magic in MAGIC:
        xor=data[0]^magic[0]
        if bytes(v^xor for v in data[:len(magic)])==magic:
            output=bytes(v^xor for v in data)
            if mime(output):return output,mime(output)
    raise ValueError('无法识别本地图片编码')


def decode_wxgf(data):
    """Convert the first HEVC frame in WeChat's WXGF container to JPEG."""
    import imageio_ffmpeg
    starts = [i for marker in (b'\x00\x00\x00\x01\x40', b'\x00\x00\x01\x40')
              if (i := data.find(marker)) >= 0]
    if not starts:
        raise ValueError('微信图片中的视频帧无法识别')
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    try:
        result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-v', 'error',
            '-protocol_whitelist', 'pipe', '-f', 'hevc', '-i', 'pipe:0', '-frames:v', '1',
            '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'],
            input=data[min(starts):], capture_output=True, timeout=30, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('微信图片转换失败或超时') from None
    if result.returncode or not result.stdout.startswith(b'\xff\xd8\xff'):
        raise ValueError('微信图片转换失败')
    return result.stdout

def find_image_key(reader=None):
    wechat = reader or globals()['wechat']
    if not wechat.root:raise ValueError('请先连接微信')
    attach=wechat.root.parent/'msg'/'attach'
    samples=[]
    for folder in attach.glob('*/*/Img'):
        for path in folder.glob('*_t.dat'):
            with path.open('rb') as stream:data=stream.read(31)
            if data.startswith(b'\x07\x08V2\x08\x07') and len(data)==31:samples.append(data[15:31])
            if len(samples)>=4:break
        if len(samples)>=4:break
    if not samples:raise ValueError('未找到本机 V2 图片样本')
    def verify(key):
        return sum(bool(mime(plain) or plain.startswith(b'wxgf')) for block in samples
                   if (plain := AES.new(key,AES.MODE_ECB).decrypt(block)))>=min(2,len(samples))
    def save(key, xor=None):
        saved=read_json('image-keys.bin',{},True);saved[str(wechat.root)]=key.hex();write_json('image-keys.bin',saved,True)
        values=read_json('image-xor-keys.bin',{},True)
        if xor is None:values.pop(str(wechat.root),None)
        else:values[str(wechat.root)]=xor
        write_json('image-xor-keys.bin',values,True)
    # Some WeChat versions expose account-specific derivation inputs in local
    # kvcomm filenames. Never trust a candidate until independent encrypted
    # samples from this account have verified it.
    appdata = os.environ.get('APPDATA')
    codes = set()
    if appdata:
        root = Path(appdata)/'Tencent'/'xwechat'
        for rel in ('net/kvcomm','net_1/kvcomm','net_2/kvcomm','ilink/kvcomm'):
            for path in (root/rel).glob('key_*.statistic'):
                match = re.fullmatch(r'key_(\d+)_.+\.statistic', path.name, re.I)
                if match:codes.add(match.group(1))
    account = wechat.root.parent.name
    names = {account}
    match = re.match(r'^(wxid_[^_]+)', account, re.I)
    if match:names.add(match.group(1))
    if re.search(r'_[a-zA-Z0-9]{4}$', account):names.add(account.rsplit('_',1)[0])
    candidates = set()
    for code in codes:
        for name in names:
            candidate=hashlib.md5((code+name).encode('utf-8')).hexdigest()[:16].encode('ascii')
            if verify(candidate):candidates.add((candidate,int(code)&255))
    if len(candidates) == 1:
        candidate,xor = candidates.pop();save(candidate,xor)
        return {'ok':True}
    if len(candidates) > 1:
        raise ValueError('找到多个图片密钥候选，未自动选择')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=wt.HANDLE
    kernel.CloseHandle.argtypes=[wt.HANDLE]
    kernel.VirtualQueryEx.argtypes=[wt.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t]
    kernel.VirtualQueryEx.restype=ctypes.c_size_t
    kernel.ReadProcessMemory.argtypes=[wt.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)]
    class Region(ctypes.Structure):
        _fields_=[('base',ctypes.c_void_p),('allocation',ctypes.c_void_p),('protect0',wt.DWORD),('partition',wt.WORD),('size',ctypes.c_size_t),('state',wt.DWORD),('protect',wt.DWORD),('type',wt.DWORD)]
    for p in sorted([p for p in psutil.process_iter(['name','memory_info']) if p.info['name']=='Weixin.exe'],key=lambda p:p.info['memory_info'].rss,reverse=True):
        handle=kernel.OpenProcess(0x410,False,p.pid)
        if not handle:continue
        try:
            region=Region();address=0;seen=set()
            while kernel.VirtualQueryEx(handle,address,ctypes.byref(region),ctypes.sizeof(region)):
                base=region.base or 0
                if region.state==0x1000 and region.protect in {4,8,64,128}:
                    for off in range(0,region.size,4*1024*1024):
                        length=min(4*1024*1024+64,region.size-off);buf=ctypes.create_string_buffer(length);n=ctypes.c_size_t()
                        kernel.ReadProcessMemory(handle,base+off,buf,length,ctypes.byref(n))
                        for match in re.finditer(rb'(?<![a-zA-Z0-9])([a-zA-Z0-9]{16}|[a-zA-Z0-9]{32})(?![a-zA-Z0-9])',buf.raw[:n.value]):
                            candidate=match[1][:16]
                            if candidate in seen:continue
                            seen.add(candidate)
                            if verify(candidate):
                                save(candidate)
                                return {'ok':True}
                nxt=base+region.size
                if nxt<=address:break
                address=nxt
        finally:kernel.CloseHandle(handle)
    raise ValueError('未找到可验证的图片密钥；可在微信中查看已有图片后重试')

def image_for(user,local_id,database,reader=None,with_tier=False):
    # Background collectors can hold several accounts at once. Resolve every
    # path, database and key against the requested reader, never the UI singleton.
    wechat = reader or globals()['wechat']
    with wechat.lock:
        if not wechat.root:raise ValueError('尚未连接')
        table='Msg_'+hashlib.md5(user.encode()).hexdigest();raw=None
        for rel,tables in wechat.tables.items():
            if table not in tables or rel!=database:continue
            con=wechat.connect_db(rel)
            try:
                row=con.execute(f'SELECT message_content,server_id,create_time FROM [{table}] WHERE local_id=? AND (local_type & 4294967295)=3',(local_id,)).fetchone()
                if row:
                    from .wechat import decode
                    raw=decode(row[0]);server_id=row[1];timestamp=row[2];break
            finally:con.close()
        if raw is None:raise ValueError('图片消息不存在')
        hashes=set(re.findall(r'(?i)[0-9a-f]{32}',raw))
        rel='message/message_resource.db'
        if rel in wechat.cache:
            con=wechat.connect_db(rel)
            try:
                # Include conversation identity where schema exposes it; local_id alone is ambiguous.
                cols={r[1] for r in con.execute('PRAGMA table_info(MessageResourceInfo)')}
                idcol='message_local_id' if 'message_local_id' in cols else 'local_id'
                clause=idcol+'=?';params=[local_id]
                if 'message_create_time' in cols:
                    clause+=' AND message_create_time=?';params.append(timestamp)
                if 'message_svr_id' in cols and server_id:
                    clause+=' AND message_svr_id=?';params.append(server_id)
                if 'chat_id' in cols and 'ChatName2Id' in wechat.tables[rel]:
                    namecols={r[1] for r in con.execute('PRAGMA table_info(ChatName2Id)')}
                    if 'user_name' in namecols:
                        clause+=' AND chat_id IN (SELECT rowid FROM ChatName2Id WHERE user_name=?)';params.append(user)
                for row in con.execute('SELECT packed_info FROM MessageResourceInfo WHERE '+clause,params):
                    hashes.update(v.decode().lower() for v in re.findall(rb'[0-9a-f]{32}',row[0] or b''))
            finally:con.close()
        base=wechat.root.parent/'msg'/'attach'/hashlib.md5(user.encode()).hexdigest()
        candidates=[]
        for value in hashes:
            candidates.extend(base.glob(f'*/Img/{value}*.dat'))
        # Prefer the original image; thumbnails are a fallback when it is absent
        # or cannot be decoded.
        candidates.sort(key=lambda p:(0 if p.name.endswith('_h.dat') else 2 if p.name.endswith('_t.dat') else 1,p.name))
        if not candidates:raise ValueError('该图片未保存在本机，当前可查看原始消息记录')
        saved=read_json('image-keys.bin',{},True).get(str(wechat.root))
        key=bytes.fromhex(saved) if saved else None
        xor=read_json('image-xor-keys.bin',{},True).get(str(wechat.root))
        if not isinstance(xor,int) or not 0<=xor<=255:xor=None
        error=None
        for path in candidates:
            if path.stat().st_size>50*1024*1024:continue
            try:
                data, kind = decode_dat(path.read_bytes(),key,xor)
                tier = 'original' if path.name.endswith('_h.dat') else 'thumbnail' if path.name.endswith('_t.dat') else 'standard'
                return (data, kind, tier) if with_tier else (data, kind)
            except ValueError as e:error=e
        raise error or ValueError('图片过大或格式暂不支持')


MAX_MEDIA_BYTES = 100 * 1024 * 1024


def resource_hashes(reader, user, item):
    """Resolve resource hashes only for the exact conversation and message."""
    rel = 'message/message_resource.db'
    if rel not in reader.cache or 'ChatName2Id' not in reader.tables.get(rel, set()):
        return set()
    con = reader.connect_db(rel)
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info(MessageResourceInfo)')}
        if not {'chat_id', 'message_local_id', 'message_create_time', 'packed_info'} <= cols:
            return set()
        rows = con.execute('SELECT packed_info FROM MessageResourceInfo WHERE chat_id IN '
            '(SELECT rowid FROM ChatName2Id WHERE user_name=?) AND message_local_id=? AND message_create_time=?',
            (user, item['local_id'], item['timestamp']))
        return {v.decode().lower() for row in rows for v in re.findall(rb'[0-9a-fA-F]{32}', row[0] or b'')}
    finally:
        con.close()


def _safe_file(path, root):
    try:
        return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _read_bounded(path):
    with path.open('rb') as stream:
        data = stream.read(MAX_MEDIA_BYTES + 1)
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError('文件超过 100 MiB，暂无法同步')
    if not data:
        raise ValueError('本机媒体文件为空')
    return data


def voice_for(reader, user, item):
    """VoiceInfo is sharded; identity requires conversation, time and both IDs."""
    from .crypto import HEADER, snapshot
    for src in sorted((reader.root / 'message').glob('media_*.db')):
        with src.open('rb') as f:
            salt = f.read(16)
        key = bytes.fromhex(reader.keys[salt.hex()]) if salt.hex() in reader.keys else None
        if salt != HEADER and key is None:
            continue
        cache = getattr(reader, '_voice_cache', None)
        if cache is None:
            cache = reader._voice_cache = {}
        stamp = (reader.fingerprint(src), reader.fingerprint(Path(str(src)+'-wal')))
        cached = cache.get(str(src))
        if not cached or cached[0] != stamp or not cached[1].exists():
            from . import storage
            folder = storage.DATA/'cache'/hashlib.sha256(str(reader.root).encode()).hexdigest()[:16]/'message'
            folder.mkdir(parents=True, exist_ok=True)
            path = folder/src.name
            temp = folder/(src.name+'.'+uuid.uuid4().hex+'.tmp')
            try:
                before = stamp
                snapshot(src, temp, key)
                after = (reader.fingerprint(src), reader.fingerprint(Path(str(src)+'-wal')))
                if before != after:
                    raise ValueError('语音数据库正在变化，请稍后重试')
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
            cache[str(src)] = (after, path)
        path = cache[str(src)][1]
        con = sqlite3.connect(path)
        try:
            query = ('SELECT voice_data FROM VoiceInfo WHERE chat_name_id IN '
                '(SELECT rowid FROM Name2Id WHERE user_name=?) AND local_id=? AND create_time=?')
            args = [user, item['local_id'], item['timestamp']]
            if item.get('server_id') not in (None, '', 0, '0'):
                query += ' AND svr_id=?'; args.append(item['server_id'])
            rows = con.execute(query, args).fetchall()
            if len(rows) > 1:
                raise ValueError('本机语音记录有歧义，未上传')
            if rows and rows[0][0]:
                data = bytes(rows[0][0])
                if len(data) > MAX_MEDIA_BYTES:
                    raise ValueError('语音超过 100 MiB，暂无法同步')
                return data, 'audio/silk', 'voice.silk'
        finally:
            con.close()
    raise ValueError('该语音未保存在本机或语音数据库未解锁')


def media_for(reader, user, database, item):
    """Read only media tied to an already-authorized message, never an input path."""
    with reader.lock:
        kind = item['type']
        if kind == 3:
            data, mime_type, tier = image_for(user, item['local_id'], database, reader=reader, with_tier=True)
            extension = {'image/jpeg':'jpg','image/png':'png','image/gif':'gif','image/webp':'webp'}[mime_type]
            return data, mime_type, 'image-'+tier+'.'+extension
        if kind == 34:
            return voice_for(reader, user, item)
        if kind == 43:
            root = reader.root.parent / 'msg' / 'video'
            hashes = resource_hashes(reader, user, item)
            candidates = {p for h in hashes for p in root.glob('*/'+h+'*.mp4') if _safe_file(p, root)}
            if not candidates:
                raise ValueError('该视频未保存在本机')
            expected = item.get('extra', {}).get('videomsg_length')
            matched = [p for p in candidates if expected and p.stat().st_size == expected]
            if len(matched) == 1:
                path = matched[0]
            elif len(candidates) == 1:
                path = next(iter(candidates))
            else:
                raise ValueError('本机视频记录有歧义，未上传')
            return _read_bounded(path), 'video/mp4', 'video.mp4'
        if kind == 49:
            from xml.etree import ElementTree as ET
            try:
                xml = ET.fromstring(item['raw'].split(':\n', 1)[-1])
                app = xml if xml.tag == 'appmsg' else xml.find('.//appmsg')
                if app is None or app.findtext('type') != '6':
                    raise ValueError('此消息不是文件附件')
                name = app.findtext('title') or app.findtext('appattach/filename') or ''
                size = int(app.findtext('appattach/totallen') or '0')
                expected_md5 = (app.findtext('md5') or '').lower()
            except (ET.ParseError, TypeError):
                raise ValueError('文件消息格式无法解析') from None
            if not name or name in ('.', '..') or '/' in name or '\\' in name or ':' in name or not size:
                raise ValueError('文件名或大小无效')
            if not re.fullmatch(r'[0-9a-f]{32}', expected_md5):
                raise ValueError('文件消息缺少可校验的内容摘要')
            root = reader.root.parent / 'msg' / 'file'
            month = datetime.fromtimestamp(item['timestamp'], ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')
            path = root / month / name
            if not _safe_file(path, root) or path.stat().st_size != size:
                raise ValueError('该文件未保存在本机或大小不匹配')
            data = _read_bounded(path)
            if hashlib.md5(data).hexdigest() != expected_md5:
                raise ValueError('本机文件与消息摘要不匹配')
            return data, mimetypes.guess_type(name)[0] or 'application/octet-stream', name
        raise ValueError('该消息不含支持的媒体')
