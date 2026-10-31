'use strict';
const $=id=>document.getElementById(id);
const state={token:'',page:'overview',account:'',overview:null,sessions:[],sessionTotal:0,user:null,epoch:0,sessionEpoch:0,messages:[],total:0,generation:null,more:false,busy:false,filter:{q:'',start:0,end:0}};
const paths={chat:'M21 11.5a8 8 0 0 1-8 8H7l-5 3 1.5-5A8 8 0 1 1 21 11.5Z',grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',plug:'M8 3v5 M16 3v5 M5 8h14 M7 8v5a5 5 0 0 0 10 0V8 M12 18v4',database:'M21 5c0 2-4 3-9 3S3 7 3 5s4-3 9-3 9 1 9 3 M3 5v14c0 2 4 3 9 3s9-1 9-3V5 M3 12c0 2 4 3 9 3s9-1 9-3',sync:'M20 7a8 8 0 0 0-14-2L3 8 M3 3v5h5 M4 17a8 8 0 0 0 14 2l3-3 M16 16h5v5',refresh:'M20 7a8 8 0 1 0 0 10 M20 2v5h-5',user:'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M4 21v-2a8 8 0 0 1 16 0v2',arrow:'M4 12h16 M14 6l6 6-6 6',eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12 M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',shield:'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6z M8 12l3 3 5-6',monitor:'M3 3h18v13H3z M12 16v5 M8 21h8',spark:'m12 3 3 6 6 3-6 3-3 6-3-6-6-3 6-3z M20 2v4 M18 4h4',search:'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0 M15 15l6 6',download:'M12 3v12 M7 10l5 5 5-5 M3 16v5h18v-5',copy:'M8 8h13v13H8z M16 8V3H3v13h5'};
function icon(name){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');svg.classList.add('icon');const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d',paths[name]||paths.chat);svg.append(p);return svg}
document.querySelectorAll('[data-icon]').forEach(el=>el.append(icon(el.dataset.icon)));
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=String(text);if(cls)el.className=cls;return el}
const time=(ts,options={})=>ts===null||ts===undefined?'—':new Date(ts*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',...options});
const day=ts=>time(ts,{year:'numeric',month:'2-digit',day:'2-digit',hour:undefined,minute:undefined,second:undefined}).split(' ')[0];
const clock=ts=>time(ts,{hour:'2-digit',minute:'2-digit',second:undefined,year:undefined,month:undefined,day:undefined});
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,3500)}
function showError(error){$('pageError').textContent=error.message||String(error);$('pageError').hidden=false}
function clearError(){$('pageError').hidden=true;$('pageError').textContent=''}
function signOut(message=''){
  state.token='';state.account='';state.overview=null;state.sessions=[];state.messages=[];state.user=null;state.epoch++;state.sessionEpoch++;state.busy=false;
  $('workspace').hidden=true;$('login').hidden=false;$('accessToken').value='';$('accessToken').type='password';$('toggleToken').setAttribute('aria-pressed','false');$('toggleToken').setAttribute('aria-label','显示读取 token');
  for(const id of ['recentSessions','sessionList','messageList','chatContext','mcpConfig'])$(id).replaceChildren();
  $('accountName').textContent='—';$('includeToken').checked=false;$('loginError').textContent=message;$('accessToken').focus();
}
async function api(path){
  const response=await fetch(new URL(path,document.baseURI),{headers:{Authorization:'Bearer '+state.token},cache:'no-store'});
  if(response.status===401){signOut('读取凭据无效或已被撤销，请重新登录。');throw new Error('读取凭据无效或已被撤销')}
  if(!response.ok){let detail;try{detail=(await response.json()).detail}catch{}throw new Error(typeof detail==='string'?detail:'读取失败，请重试（HTTP '+response.status+'）')}
  return response.json();
}
function empty(title,description){const e=node('div',undefined,'empty-list');e.append(icon('chat'),node('h3',title),node('p',description));return e}
function avatar(session){return node('span',session.name?.slice(0,1)||'聊','avatar'+(session.group?' group':''))}
function setService(online){$('serviceBadge').classList.toggle('stale',!online);$('serviceLabel').textContent=online?'服务运行中':'暂时无法连接'}
async function refreshOverview(){
  const token=state.token;
  const [overview,recent]=await Promise.all([api('api/console/overview'),api('api/external/sessions?limit=5')]);
  if(!token||state.token!==token)return;
  state.overview=overview;state.account=overview.account;setService(true);
  $('accountName').textContent=overview.account;$('sessionMetric').textContent=overview.workspace_count.toLocaleString();$('messageMetric').textContent=overview.message_count.toLocaleString();
  $('messageRange').textContent=overview.message_count?day(overview.first_message)+' — '+day(overview.last_message):'等待客户端上传';
  $('syncMetric').textContent=overview.updated_at?time(overview.updated_at,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:undefined,year:undefined}):'尚未同步';
  $('syncDetail').textContent=overview.updated_at?'最近一次服务端接收时间':'尚未接收到上传';
  $('checkedAt').textContent='最近检查 '+time(overview.checked_at)+' · 北京时间';$('onboarding').hidden=overview.workspace_count>0;
  $('recentSessions').replaceChildren();
  for(const s of recent.items){const button=node('button',undefined,'recent-row');button.type='button';const body=node('div',undefined,'session-body');body.append(node('strong',s.name),node('p',s.note||(s.group?'群聊':'私聊')+' · '+s.id));button.append(avatar(s),body,node('time',s.timestamp?day(s.timestamp):'暂无消息'));button.onclick=()=>openSession(s);$('recentSessions').append(button)}
  if(!recent.items.length)$('recentSessions').append(empty('还没有已同步的会话','在客户端选择对话并开启上传后，会话会显示在这里。'));
}
async function go(page){
  state.page=page;clearError();
  for(const p of ['overview','conversations','integration'])$(p+'Page').hidden=p!==page;
  document.querySelectorAll('[data-page]').forEach(b=>{const selected=b.dataset.page===page;b.classList.toggle('selected',selected);selected?b.setAttribute('aria-current','page'):b.removeAttribute('aria-current')});
  $('pageCrumb').textContent={overview:'服务总览',conversations:'会话与记录',integration:'连接与 AI 接入'}[page];
  try{if(page==='overview')await refreshOverview();if(page==='conversations')await loadSessions();if(page==='integration')renderConnection()}catch(e){showError(e);setService(false)}
}
document.querySelectorAll('[data-page],[data-goto]').forEach(b=>b.onclick=()=>go(b.dataset.page||b.dataset.goto));
$('loginForm').onsubmit=async event=>{
  event.preventDefault();const token=$('accessToken').value.trim();$('loginError').textContent='';
  if(token.length<32||!/^[\x21-\x7e]+$/.test(token)){$('loginError').textContent='请输入完整的读取 token。';return}
  $('loginButton').disabled=true;state.token=token;
  try{await refreshOverview();if(!state.token)return;$('accessToken').value='';$('login').hidden=true;$('workspace').hidden=false;await go('overview')}
  catch(e){state.token='';$('loginError').textContent=e.message||'无法连接服务端，请重试。'}
  finally{$('loginButton').disabled=false}
};
$('toggleToken').onclick=()=>{const visible=$('accessToken').type==='password';$('accessToken').type=visible?'text':'password';$('toggleToken').setAttribute('aria-pressed',String(visible));$('toggleToken').setAttribute('aria-label',visible?'隐藏读取 token':'显示读取 token')};
$('logout').onclick=()=>signOut();
$('refreshOverview').onclick=async()=>{const b=$('refreshOverview');b.disabled=true;clearError();try{await refreshOverview();toast('已更新服务端状态')}catch(e){showError(e);setService(false)}finally{b.disabled=false}};
function clearChat(){state.user=null;state.epoch++;state.busy=false;state.messages=[];state.total=0;state.generation=null;$('chatEmpty').hidden=false;$('chatContent').hidden=true;$('messageList').replaceChildren();$('chatContext').replaceChildren();renderSessions()}
async function loadSessions(more=false){
  const epoch=++state.sessionEpoch,token=state.token;
  const result=await api('api/external/sessions?'+new URLSearchParams({q:$('sessionSearch').value.trim(),offset:more?state.sessions.length:0,limit:50}));
  if(epoch!==state.sessionEpoch||state.token!==token)return;
  state.sessions=more?[...state.sessions,...result.items]:result.items;state.sessionTotal=result.total;renderSessions();
}
function renderSessions(){
  const fragment=document.createDocumentFragment();
  for(const s of state.sessions){const b=node('button',undefined,'session-button'+(state.user?.id===s.id?' selected':''));b.setAttribute('aria-pressed',String(state.user?.id===s.id));const body=node('div',undefined,'session-body');body.append(node('strong',s.name),node('p',s.note||s.preview||s.id));b.append(avatar(s),body);b.onclick=()=>selectSession(s).catch(showError);fragment.append(b)}
  if(!state.sessions.length)fragment.append(empty($('sessionSearch').value?'没有匹配的会话':'还没有会话','会话由客户端选择并同步到服务端。'));
  $('sessionList').replaceChildren(fragment);$('sessionTotal').textContent=state.sessionTotal;$('moreSessions').hidden=state.sessions.length>=state.sessionTotal;
}
async function openSession(session){$('sessionSearch').value='';await go('conversations');try{await selectSession(session)}catch(e){showError(e)}}
async function selectSession(session){
  clearError();state.user=session;const epoch=++state.epoch;state.busy=false;state.messages=[];state.total=0;state.generation=null;resetFilters();
  $('chatEmpty').hidden=true;$('chatContent').hidden=false;$('chatName').textContent=session.name;$('chatId').textContent=(session.group?'群聊':'私聊')+' · '+session.id;$('contextDetails').open=false;$('chatContext').replaceChildren();$('messageList').replaceChildren(empty('正在读取记录','从服务端加载已保存的聊天。'));renderSessions();
  await Promise.all([loadMessages(),api('api/external/context?'+new URLSearchParams({user:session.id})).then(context=>{if(state.epoch===epoch)renderContext(context)})]);
}
function renderContext(context){const root=$('chatContext');root.replaceChildren();const labels={note:'会话备注',profile:'背景设定',latest_summary:'最近总结',knowledge:'已确认知识',period_summaries:'周期总结'};
  for(const [key,title] of Object.entries(labels)){const value=context[key];if(value===null||value===undefined||value===''||typeof value==='object'&&!Object.keys(value).length)continue;const block=node('div',undefined,'context-block');block.append(node('h3',title),node('pre',typeof value==='string'?value:JSON.stringify(value,null,2)));root.append(block)}
  if(!root.childNodes.length)root.append(node('p','客户端尚未上传此会话的备注或背景。'));
}
function resetFilters(){$('messageSearch').value='';$('dateStart').value='';$('dateEnd').value='';state.filter={q:'',start:0,end:0}}
async function loadMessages(more=false){
  if(!state.user||state.busy)return;state.busy=true;const epoch=state.epoch,user=state.user.id;
  const area=$('messageArea'),previousHeight=area.scrollHeight,previousTop=area.scrollTop;
  $('olderMessages').disabled=true;$('exportMessages').disabled=true;
  try{
    const result=await api('api/external/messages?'+new URLSearchParams({user,offset:more?state.messages.length:0,limit:100,...state.filter}));
    if(epoch!==state.epoch)return;
    if(more&&state.generation!==result.generation){state.busy=false;await loadMessages(false);toast('同步期间记录有更新，已重新读取最新一页');return}
    state.messages=more?[...result.items,...state.messages]:result.items;state.total=result.total;state.more=result.has_more;state.generation=result.generation;renderMessages();
    area.scrollTop=more?area.scrollHeight-previousHeight+previousTop:area.scrollHeight;
  }catch(e){if(epoch===state.epoch){if(e.message.includes('未加入'))clearChat();else if(!state.messages.length)$('messageList').replaceChildren(empty('记录读取失败',e.message));showError(e)}}
  finally{if(epoch===state.epoch){state.busy=false;$('olderMessages').disabled=false;$('exportMessages').disabled=!state.messages.length}}
}
function appendSharedLink(container,m){if(m.type!==49||!m.extra)return;const {des,url}=m.extra;if(des&&des!==m.text)container.append(node('div',des));if(typeof url!=='string'||!url)return;let parsed;try{parsed=new URL(url);if(!['http:','https:'].includes(parsed.protocol)||parsed.username||parsed.password)throw new Error()}catch{container.append(node('div',url));return}const a=node('a',url);a.href=parsed.href;a.target='_blank';a.rel='noopener noreferrer';a.referrerPolicy='no-referrer';container.append(a)}
function renderMessages(){
  const fragment=document.createDocumentFragment();let previous='';
  for(const m of state.messages){const date=day(m.timestamp);if(date!==previous){fragment.append(node('div',date,'message-day'));previous=date}const row=node('article',undefined,'message'+(m.is_me?' me':''));row.append(node('span',m.is_me?'我':(m.sender||'未知').slice(0,1),'avatar'));const body=node('div',undefined,'message-body'),meta=node('div',undefined,'message-meta');meta.append(node('span',m.sender||m.sender_id||'未知发送者'),node('time',clock(m.timestamp)));const bubble=node('div',undefined,'message-bubble');if(m.type!==1)bubble.append(node('span',m.type_name||'非文本记录','message-type'));bubble.append(node('div',m.text||''));appendSharedLink(bubble,m);body.append(meta,bubble);if(m.type!==1){const raw=node('details');raw.append(node('summary','原始记录'),node('pre',m.raw||''));body.append(raw)}row.title='消息 ID：'+m.id;row.append(body);fragment.append(row)}
  if(!state.messages.length)fragment.append(empty('没有符合条件的记录','试试其他关键词或日期范围。'));
  $('messageList').replaceChildren(fragment);$('olderMessages').hidden=!state.more;$('messageCount').textContent=`已显示 ${state.messages.length.toLocaleString()} / ${state.total.toLocaleString()} 条`;
}
$('sessionSearchForm').onsubmit=event=>{event.preventDefault();loadSessions().catch(showError)};
$('sessionSearch').oninput=()=>{clearTimeout(loadSessions.timer);loadSessions.timer=setTimeout(()=>loadSessions().catch(showError),250)};
$('moreSessions').onclick=async()=>{const b=$('moreSessions');b.disabled=true;try{await loadSessions(true)}catch(e){showError(e)}finally{b.disabled=false}};
$('messageSearchForm').onsubmit=event=>{event.preventDefault();clearError();const start=$('dateStart').value?new Date($('dateStart').value+'T00:00:00+08:00').getTime()/1000:0,end=$('dateEnd').value?new Date($('dateEnd').value+'T23:59:59+08:00').getTime()/1000:0;if(start&&end&&end<start){showError(new Error('结束日期不能早于开始日期。'));return}state.epoch++;state.busy=false;state.filter={q:$('messageSearch').value.trim(),start,end};loadMessages()};
$('resetMessages').onclick=()=>{resetFilters();state.epoch++;state.busy=false;loadMessages()};
$('olderMessages').onclick=()=>loadMessages(true);
$('refreshConversations').onclick=async()=>{const b=$('refreshConversations');b.disabled=true;clearError();try{await loadSessions();if(state.user){state.epoch++;state.busy=false;await loadMessages();if(state.user)renderContext(await api('api/external/context?'+new URLSearchParams({user:state.user.id})))}await refreshOverview();toast('已更新会话与记录')}catch(e){showError(e)}finally{b.disabled=false}};
$('exportMessages').onclick=()=>{if(!state.messages.length)return;const url=URL.createObjectURL(new Blob([JSON.stringify(state.messages,null,2)],{type:'application/json'})),link=node('a');link.href=url;link.download='聊天记录.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('已导出当前显示的 '+state.messages.length+' 条记录')};
function renderConnection(){const server=new URL('.',document.baseURI).href.replace(/\/$/,''),mcp=server+'/mcp';$('serverUrl').value=server;$('mcpUrl').value=mcp;const token=$('includeToken').checked?state.token:'<read_token>';$('mcpConfig').textContent=JSON.stringify({mcpServers:{'wechat-helper':{url:mcp,headers:{Authorization:'Bearer '+token}}}},null,2)}
async function copy(text){try{await navigator.clipboard.writeText(text);toast('已复制')}catch{toast('浏览器不允许自动复制，请选取内容后手动复制。')}}
$('includeToken').onchange=renderConnection;$('copyServerUrl').onclick=()=>copy($('serverUrl').value);$('copyMcpUrl').onclick=()=>copy($('mcpUrl').value);$('copyMcpConfig').onclick=()=>copy($('mcpConfig').textContent);
setInterval(async()=>{if(!state.token||document.hidden||!$('autoRefresh').checked)return;try{await refreshOverview()}catch(e){if(state.token){setService(false);showError(e)}}},15000);
