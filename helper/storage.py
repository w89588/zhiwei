import ctypes
import ctypes.wintypes as wt
import json
import os
import threading
import uuid
from pathlib import Path

DATA = Path(os.environ.get('WECHAT_HELPER_DATA', Path(os.environ.get('LOCALAPPDATA', '.')) / 'Zhiwei'))
DATA.mkdir(parents=True, exist_ok=True)
WRITE_LOCK = threading.RLock()

def protect(data, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [('size',wt.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    raw = ctypes.create_string_buffer(data)
    inp = Blob(len(data),ctypes.cast(raw,ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(inp),None,None,None,None,1,ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.data,out.size)
    finally:
        ctypes.windll.kernel32.LocalFree(out.data)

def read_json(name, default, secret=False):
    path = DATA/name
    if not path.exists():
        return default
    raw = path.read_bytes()
    return json.loads((protect(raw,True) if secret else raw).decode('utf-8'))

def write_json(name, value, secret=False):
    raw = json.dumps(value,ensure_ascii=False).encode('utf-8')
    path = DATA/name
    with WRITE_LOCK:
        temp = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        try:
            temp.write_bytes(protect(raw) if secret else raw)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
