"""Recovery checks for the dedicated QA records created by verify_wslc_v2.py."""
import json
from pathlib import Path
import subprocess
import time
import httpx

root=Path(__file__).resolve().parents[1]
qa=json.loads((root/'.private/wslc-v2/qa.json').read_text('utf-8'))


def run(*args):
    subprocess.run(['wslc',*args],check=True,capture_output=True)


def wait():
    for _ in range(30):
        try:
            with httpx.Client(trust_env=False,timeout=10) as c:
                r=c.get('http://127.0.0.1:8787/readyz')
            if r.status_code==200:return r.json()
        except httpx.HTTPError:pass
        time.sleep(1)
    raise AssertionError('WSLC server did not become ready')


def snapshot():
    with httpx.Client(base_url='http://127.0.0.1:8787',headers={'X-Wechat-UI':'1'},trust_env=False,timeout=15) as c:
        r=c.post('/api/v2/auth/login',json={'username':qa['username'],'password':qa['password']})
        assert r.status_code==200
        overview=c.get('/api/v2/overview').json()
        assert overview['messages']==2 and overview['memories']==7,overview
        item=c.get('/api/v2/memories').json()['items'][0]
        memory=c.get('/api/v2/memories/'+item['id']).json()
        assert '测试' in memory['markdown']
        return overview['messages'],overview['memories'],memory['id'],memory['markdown']


before=snapshot()
run('restart','wechat-v2-server')
assert wait()['ok'] and snapshot()==before
try:
    run('stop','wechat-v2-redis')
    health=wait()
    assert health['degraded'] and health['services']['redis']=='unavailable'
    assert snapshot()==before
finally:
    run('start','wechat-v2-redis')
assert wait()['services']['redis']=='ok'
report={'server_restart_preserves_messages_and_memory':True,'redis_outage_does_not_block_reads_or_login':True,'redis_recovery':True}
(root/'.private/wslc-v2/recovery.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report))
