"""Check the Windows worker artifact without hiding windows at launch."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import uuid


def verify(executable, test_root):
    executable=Path(executable).resolve()
    with executable.open('rb') as stream:
        assert stream.read(2)==b'MZ', 'Not a Windows executable'
        stream.seek(0x3c)
        pe_offset=struct.unpack('<I',stream.read(4))[0]
        stream.seek(pe_offset)
        assert stream.read(4)==b'PE\0\0', 'Missing PE header'
        stream.seek(pe_offset+24+68)
        subsystem=struct.unpack('<H',stream.read(2))[0]
    assert subsystem==2, 'Background worker must use Windows GUI subsystem (no console)'
    root=Path(test_root).resolve()/('worker-'+uuid.uuid4().hex)
    results={}
    for name in ('empty','error'):
        data=root/name
        data.mkdir(parents=True)
        if name=='error':(data/'source.json').write_text('{invalid',encoding='utf-8')
        # No CREATE_NO_WINDOW or STARTUPINFO: the artifact itself must be silent.
        completed=subprocess.run([str(executable),'--once','--data',str(data)],
                                 env={**os.environ,'WECHAT_HELPER_DATA':str(data)},
                                 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,timeout=60)
        expected=0 if name=='empty' else 1
        assert completed.returncode==expected, (name,completed.returncode)
        diagnostic=data/'background-error.log'
        assert diagnostic.exists()==(name=='error'), name
        if diagnostic.exists():
            assert diagnostic.read_text('utf-8')=='后台周期整理失败，请在应用的自动化页面检查设置。'
        results[name]='passed'
    report={'executable':str(executable),'subsystem':subsystem,**results}
    (root/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    print(json.dumps(verify(sys.argv[1],sys.argv[2]),ensure_ascii=False))
