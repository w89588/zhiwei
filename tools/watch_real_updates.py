import httpx,re,time,json
url='http://127.0.0.1:8765'
client=httpx.Client(timeout=30,trust_env=False)
token=re.search(r"window.HELPER_TOKEN='([^']+)'",client.get(url).text)[1]
headers={'X-Helper-Token':token}
initial=client.get(url+'/api/status',headers=headers).json()
print('Initial generation',initial['generation'],flush=True)
for _ in range(90):
    time.sleep(2)
    current=client.get(url+'/api/status',headers=headers).json()
    if current['generation']>initial['generation']:
        print(json.dumps({'live_update':'passed','before':initial['generation'],'after':current['generation'],'errors':current['errors']},ensure_ascii=True),flush=True)
        with open('.private/live-verification.json','w',encoding='utf-8') as out:json.dump({'live_update':'passed','before':initial['generation'],'after':current['generation'],'errors':current['errors']},out,ensure_ascii=False,indent=2)
        break
else:print('No source change observed during the verification window',flush=True)
