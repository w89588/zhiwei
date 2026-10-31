"""Read-only WCDB SQLCipher 4 reader. Source databases are never modified."""
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import re
import struct
import shutil
from pathlib import Path
import psutil
from Crypto.Cipher import AES

PAGE = 4096
HEADER = b'SQLite format 3\0'

def mac_key(key, salt):
    return hashlib.pbkdf2_hmac('sha512', key, bytes(x ^ 0x3a for x in salt), 2, 32)

def decrypt_page(data, number, key, mac):
    if len(data) != PAGE:
        raise ValueError('数据库页不完整')
    start = 16 if number == 1 else 0
    # Validate every page, including WAL pages, before exposing plaintext.
    digest = hmac.new(mac, data[start:PAGE-64] + struct.pack('<I', number), hashlib.sha512).digest()
    if not hmac.compare_digest(digest, data[-64:]):
        raise ValueError(f'第 {number} 页校验失败，请重试同步')
    plain = AES.new(key, AES.MODE_CBC, data[PAGE-80:PAGE-64]).decrypt(data[start:PAGE-80])
    return (HEADER if number == 1 else b'') + plain + bytes(80)

def valid_key(page, key):
    try:
        decrypt_page(page, 1, key, mac_key(key, page[:16]))
        return True
    except ValueError:
        return False

def find_keys(root, progress=lambda value: None):
    """Find only DB keys matched and authenticated against this account's salts."""
    pages = {}
    for path in root.rglob('*.db'):
        with path.open('rb') as stream:
            page = stream.read(PAGE)
        if len(page) == PAGE and not page.startswith(HEADER):
            pages.setdefault(page[:16].hex(), page)
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.OpenProcess.restype = wt.HANDLE
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    class Region(ctypes.Structure):
        _fields_ = [('base',ctypes.c_void_p),('allocation',ctypes.c_void_p),('protect0',wt.DWORD),('partition',wt.WORD),('size',ctypes.c_size_t),('state',wt.DWORD),('protect',wt.DWORD),('type',wt.DWORD)]
    processes = []
    for p in psutil.process_iter(['name','memory_info']):
        if (p.info['name'] or '').lower() == 'weixin.exe':
            processes.append(p)
    processes.sort(key=lambda p:p.info['memory_info'].rss, reverse=True)
    if not processes:
        raise RuntimeError('请启动微信并登录，再连接本机')
    found = {}
    needle = b'com.Tencent.WCDB.Config.Cipher'
    mask = bytes.fromhex('d2c7442458020000004889442450488b450048844c2448488944254048584c24')
    for process in processes:
        handle = k32.OpenProcess(0x410, False, process.pid)
        if not handle:
            continue
        try:
            address = 0
            region = Region()
            regions = []
            while address < 0x7fffffffffff:
                if not k32.VirtualQueryEx(handle,address,ctypes.byref(region),ctypes.sizeof(region)):
                    break
                base, size = region.base or 0, region.size
                if region.state == 0x1000 and region.protect in {2,4,8,32,64,128}:
                    regions.append((base,size))
                next_address = base + size
                if next_address <= address:
                    break
                address = next_address
            def read_at(address,length):
                buf=ctypes.create_string_buffer(length); read=ctypes.c_size_t()
                k32.ReadProcessMemory(handle,address,buf,length,ctypes.byref(read))
                return buf.raw[:read.value]
            def memory_chunks():
                for base,size in regions:
                    for offset in range(0, size, 4*1024*1024):
                        length = min(4*1024*1024+256,size-offset)
                        yield base+offset,read_at(base+offset,length)
            def accept(value):
                key=bytes.fromhex(value[:64]); embedded=value[64:96]
                salts=[embedded] if embedded in pages else list(pages)
                for salt in salts:
                    if salt not in found and valid_key(pages[salt],key):
                        found[salt]=key.hex()
                        progress(f'已验证 {len(found)}/{len(pages)} 个数据库密钥')
            # 4.1 stores obfuscated Config.Cipher buffers. Follow bounded,
            # validated pointers; use HMAC against the chosen account as proof.
            progress('扫描微信 4.1 运行时数据库配置')
            addresses=set()
            for base,data in memory_chunks():
                pos=data.find(needle)
                while pos>=0:
                    addresses.add(base+pos);pos=data.find(needle,pos+1)
            pairs=[struct.pack('<QQ',addr,len(needle)) for addr in addresses]
            seen=set()
            for base,data in memory_chunks():
                if len(found)==len(pages):break
                for pair in pairs:
                    pos=data.find(pair)
                    while pos>=0:
                        node=read_at(base+pos-16,80)
                        if len(node)==80:
                            config=struct.unpack_from('<Q',node,40)[0]
                            if 0x10000<=config<0x7fffffffffff and config not in seen:
                                seen.add(config)
                                obj=read_at(config+0x88,40)
                                if len(obj)==40:
                                    pointer,length=struct.unpack_from('<QQ',obj,8)
                                    if 0x10000<=pointer<0x7fffffffffff and 0<length<=1024:
                                        blob=read_at(pointer,length)
                                        decoded=bytes(v^mask[i%len(mask)] for i,v in enumerate(blob))
                                        for match in re.finditer(rb"[xX]'([0-9a-fA-F]{64,192})'",decoded):
                                            value=match[1].decode().lower()
                                            accept(value)
                        pos=data.find(pair,pos+1)
            if len(found)<len(pages):
                progress(f'4.1 已验证 {len(found)} 个密钥，检查旧版缓存')
                for base,data in memory_chunks():
                        for match in re.finditer(rb"[xX]'([0-9a-fA-F]{64,192})'",data):
                            value = match[1].decode().lower()
                            accept(value)
        finally:
            k32.CloseHandle(handle)
        if len(found) == len(pages):
            break
    if not found:
        raise RuntimeError('未找到可验证的数据库密钥。请确认微信已登录且本程序具有读取微信进程的权限')
    return found

def wal_checksum(data, state, endian):
    a,b = state
    for x,y in struct.iter_unpack(endian+'II',data):
        a = (a+x+b)&0xffffffff
        b = (b+y+a)&0xffffffff
    return a,b

def snapshot(source, target, key=None, base_cache=None):
    """Authenticate base pages and replay only checksum-valid committed WAL frames."""
    with source.open('rb') as stream:
        first = stream.read(PAGE)
    plain = first.startswith(HEADER)
    if not plain and key is None:
        raise RuntimeError('数据库尚未解锁')
    mac = mac_key(key,first[:16]) if not plain else None
    if base_cache and base_cache.exists():
        # Caller must verify that the base DB fingerprint is unchanged.
        shutil.copyfile(base_cache,target)
    else:
        with source.open('rb') as inp, target.open('wb') as out:
            number = 0
            while data := inp.read(PAGE):
                number += 1
                out.write(data if plain else decrypt_page(data,number,key,mac))
    wal = Path(str(source)+'-wal')
    if not wal.exists() or wal.stat().st_size < 32:
        return
    with wal.open('rb') as stream, target.open('r+b') as out:
        header = stream.read(32)
        magic, version, size = struct.unpack('>III',header[:12])
        if magic not in {0x377f0682,0x377f0683} or size != PAGE:
            raise ValueError('不支持的 WAL 格式')
        endian = '<' if magic == 0x377f0682 else '>'
        checksum = wal_checksum(header[:24],(0,0),endian)
        if checksum != struct.unpack('>II',header[24:]):
            raise ValueError('WAL 头校验失败')
        pending = []
        while frame := stream.read(PAGE+24):
            if len(frame) != PAGE+24 or frame[8:16] != header[16:24]:
                break
            checksum = wal_checksum(frame[:8]+frame[24:],checksum,endian)
            if checksum != struct.unpack('>II',frame[16:24]):
                break
            number, commit_size = struct.unpack('>II',frame[:8])
            if not number:
                break
            data = frame[24:]
            pending.append((number,data if plain else decrypt_page(data,number,key,mac)))
            if commit_size:
                for pg, content in pending:
                    out.seek((pg-1)*PAGE)
                    out.write(content)
                out.truncate(commit_size*PAGE)
                pending.clear()
