"""End-to-end real account verification; results contain counts, never chat text."""
import asyncio
import json
import time
from pathlib import Path
from helper.wechat import wechat,discover
from helper.ai import summarize,advise
from helper.storage import read_json,DATA

async def run():
    started=time.monotonic();wechat.connect(discover()[0]['path'])
    sessions=wechat.workspace_sessions()
    report={'status':wechat.status.copy(),'sessions':len(sessions),'databases':len(wechat.cache)}
    chosen=None
    for s in sessions:
        if s['group'] or s['id'].startswith('gh_') or s['id'] in {'brandsessionholder','weixin','filehelper'}:continue
        page=wechat.messages(s['id'],0,30)
        if page['total']>=30 and any(m['is_me'] for m in page['items']):
            chosen=s;break
    assert chosen,'请先在工作台添加一段包含双方消息的会话'
    first=wechat.messages(chosen['id'],0,30);older=wechat.messages(chosen['id'],30,30)
    assert not set(m['id'] for m in first['items'])&set(m['id'] for m in older['items'])
    report.update(message_count=first['total'],pagination='passed',self_sender='passed',types=list(set(m['type'] for m in first['items'])))
    profile=read_json('profiles.json',{}).get(chosen['id'],{'relationship':'朋友','style':'自然、简洁','background':'','goals':'尊重对方，澄清问题','boundaries':'避免编造承诺'})
    print('Reading and pagination passed',flush=True)
    summary=await summarize(first['items'],profile,lambda v:print(v,flush=True))
    assert len(summary)>40
    result=await advise(first['items'],profile,summary,'依据最新对话给出自然、尊重双方的回复建议')
    assert len(result.get('replies',[]))==3
    assert all(r.get('text') and r.get('reason') for r in result['replies'])
    report.update(ai_summary='passed',ai_summary_chars=len(summary),ai_replies=len(result['replies']))
    # Verify live updates without sending or changing any WeChat messages.
    initial=wechat.status['generation']
    for _ in range(10):
        await asyncio.sleep(2)
        await asyncio.to_thread(wechat.sync)
        if wechat.status['generation']>initial:break
    report['live_generation_change']=wechat.status['generation']-initial
    report['final_errors']=wechat.status['errors']
    report['elapsed_seconds']=round(time.monotonic()-started,1)
    Path('.private/real-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True),flush=True)

if __name__=='__main__':asyncio.run(run())
