import asyncio,json,time
from pathlib import Path
from helper.wechat import wechat,discover
from helper.server import run_ai,Analysis,jobs,history

async def await_job(key):
    while jobs[key]['state']=='running':await asyncio.sleep(.5)
    if jobs[key]['state']!='done':raise RuntimeError(jobs[key]['message'])
    return jobs[key]

async def main():
    wechat.connect(discover()[0]['path'])
    chosen=None;count=0
    for s in wechat.workspace_sessions():
        if s['group'] or s['id'].startswith('gh_') or s['id'] in {'weixin','filehelper','brandsessionholder'}:continue
        page=wechat.messages(s['id'],0,10)
        if 30<=page['total']<=80:
            chosen=s['id'];count=page['total'];break
    assert chosen,'请先在工作台添加一段包含 30–80 条消息的会话'
    key=(await run_ai(Analysis(user=chosen,action='summary',scope='all')))['id']
    result=await await_job(key)
    saved=history(chosen)[-1]
    assert result['count']==count==saved['count'] and saved['scope']=='all'
    assert saved['first']<=saved['last']
    key=(await run_ai(Analysis(user=chosen,action='advice',scenario='以自然、简洁、尊重双方的方式回应最新对话')))['id']
    advice=await await_job(key)
    assert len(advice['result']['replies'])==3
    report={'all_history_job':'passed','count':count,'persistence':'passed','advice_with_saved_summary':'passed','reply_count':3,'temporary_analysis_files':len(list(Path.home().joinpath('AppData/Local/WechatHelper').glob('analysis-*.txt')))}
    Path('.private/job-verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)

if __name__=='__main__':asyncio.run(main())
