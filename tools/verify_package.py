import sys,re,json
from pathlib import Path
import httpx

def main():
    url='http://127.0.0.1:'+sys.argv[1]
    client=httpx.Client(timeout=120,trust_env=False)
    html=client.get(url).text
    token=re.search(r"window.HELPER_TOKEN='([^']+)'",html)[1]
    client.headers['X-Helper-Token']=token
    status=client.get(url+'/api/status').json()
    assert status['state']=='ready' and not status['errors']
    sessions=client.get(url+'/api/sessions').json()
    report={'native_executable':'passed','databases':status['databases'],'sessions':len(sessions),'ai_model':client.get(url+'/api/ai/config').json()['model']}
    test=client.post(url+'/api/ai/test',json={});test.raise_for_status();assert test.json()['ok']
    report['packaged_ollama_connection']='passed'
    for session in sessions[:15]:
        page=client.get(url+'/api/messages',params={'user':session['id'],'limit':50}).json()
        image=next((m for m in page['items'] if m['type']==3),None)
        if image:
            response=client.get(url+'/api/image',params={'user':session['id'],'local_id':image['local_id'],'database':image['id'].split(':')[0]})
            if response.status_code==200:
                assert response.headers['content-type'].startswith('image/')
                report['packaged_image_decode']='passed';break
    assert report.get('packaged_image_decode')=='passed'
    for session in sessions:
        if session['group'] or session['id'].startswith('gh_'):continue
        page=client.get(url+'/api/messages',params={'user':session['id'],'limit':1}).json()
        if 10<=page['total']<=80:
            exported=client.get(url+'/api/export',params={'user':session['id']});exported.raise_for_status()
            assert len(exported.json())==page['total'];report['packaged_export']='passed';break
    assert report.get('packaged_export')=='passed'
    Path('.private/package-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)

if __name__=='__main__':main()
