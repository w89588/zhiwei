/* Product 1.0: evidence-backed memory, journals, calendar and automation. */
const product={view:'chat',user:null,facts:[],own:[],events:[],month:beijingToday().slice(0,7),request:0,workspacePath:null};
const categoryNames={identity:'人物信息',personality:'性格与行为',preferences:'偏好习惯',style:'表达风格',relationship:'关系背景',boundaries:'边界与禁忌',goals:'目标与关注'};
const periodNames={day:'日记',week:'周记',month:'月记',all:'完整回顾',calendar:'日历同步',automation:'周期整理',connection:'账号连接'};
const statusNames={confirmed:'已确认',pending:'待确认',rejected:'已忽略',draft:'待确认',ignored:'已忽略',done:'已完成',unchanged:'无新变化',empty:'无消息',error:'失败',running:'执行中',cancelled:'已取消'};
function beijingToday(){return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())}
function productStatus(id,text,error=false){const root=$(id);root.hidden=false;root.className='productstatus'+(error?' error':'');root.textContent=text}
function metrics(root,values){$(root).replaceChildren(...values.map(([label,value,detail])=>{const card=node('div',undefined,'metriccard');card.append(node('span',label),node('strong',value),node('small',detail));return card}))}
function emptyMemory(text){const root=node('div',undefined,'memory-empty');root.append(icon('book'),node('h3',text),node('p','整理对话后，信息会带着原文依据出现在这里。也可以先手动补充。'));return root}
async function setProductView(view){
  product.view=view;document.body.dataset.view=view;toggleAssistant(false);
  for(const name of ['knowledge','self','calendar','automation'])$(name+'Page').hidden=name!==view;
  if(view!=='chat')$(view+'Page').scrollTop=0;
  for(const [id,name] of Object.entries({workbenchBtn:'chat',knowledgeNav:'knowledge',selfNav:'self',calendarNav:'calendar',automationNav:'automation'})){
    $(id).classList.toggle('active',name===view);$(id).setAttribute('aria-current',name===view?'page':'false');
  }
  product.request++;
  try{await refreshProductPage()}catch(error){toast(error.message)}
}
async function refreshProductPage(){
  if(product.view==='knowledge')await loadPersonKnowledge();
  else if(product.view==='self')await loadSelfKnowledge();
  else if(product.view==='calendar')await loadCalendar();
  else if(product.view==='automation')await loadAutomation();
}
let workspaceRefresh;
function workspaceUpdated(){
  if(product.workspacePath!==state.path){product.workspacePath=state.path;product.user=null;product.facts=[];product.own=[];product.events=[];product.request++;for(const id of ['factDialog','eventDialog','caldavDialog'])$(id).close()}
  if(product.user&&!state.sessions.some(s=>s.id===product.user))product.user=null;
  clearTimeout(workspaceRefresh);workspaceRefresh=setTimeout(()=>refreshProductPage().catch(e=>toast(e.message)),150);
}
function evidenceBlock(evidence){
  const details=node('details',undefined,'evidence');details.append(node('summary','聊天依据 · '+evidence.length+' 条'));
  for(const item of evidence){
    const source=node('div',undefined,'evidence-item');
    source.append(node('small',item.sender+' · '+new Date(item.timestamp*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})),node('blockquote',item.excerpt||''),node('code',item.id));
    const view=node('button','查看原对话');view.onclick=()=>openEvidence(item).catch(e=>toast(e.message));source.append(view);details.append(source);
  }
  return details;
}
async function openEvidence(evidence){
  const session=state.sessions.find(s=>s.id===evidence.user);if(!session)throw new Error('来源会话已移出工作台');
  await setProductView('chat');await selectChat(session);
  const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(evidence.timestamp*1000));
  $('dateStart').value=$('dateEnd').value=day;$('messageSearch').value=(evidence.excerpt||'').slice(0,32);
  state.filter={q:$('messageSearch').value,...dateRange()};await loadMessages();
  document.querySelector('.chat').scrollIntoView({block:'start'});
}
function factCard(fact){
  const card=node('article',undefined,'factcard '+fact.status),head=node('div',undefined,'facthead');
  head.append(node('span',categoryNames[fact.category]||fact.category,'categorychip'),node('span',statusNames[fact.status],'statuschip '+fact.status));
  card.append(head,node('p',fact.text,'facttext'));
  const meta=node('div',undefined,'factmeta');meta.append(node('small',fact.certainty==='manual'?'手动记录':fact.certainty==='inference'?'AI 归纳 · 包含推测':'AI 提取 · 来自明确陈述'));
  const edit=node('button','编辑');edit.onclick=()=>openFact(fact.subject,fact);meta.append(edit);card.append(meta);
  if(fact.evidence.length)card.append(evidenceBlock(fact.evidence));
  if(fact.status==='pending'){
    const actions=node('div',undefined,'factactions'),confirm=node('button','确认信息','primary'),ignore=node('button','忽略');
    confirm.onclick=()=>changeFactStatus(fact,'confirmed',confirm);ignore.onclick=()=>changeFactStatus(fact,'rejected',ignore);actions.append(confirm,ignore);card.append(actions);
  }
  return card;
}
async function changeFactStatus(fact,status,button){
  button.disabled=true;
  try{await api('knowledge/facts/'+fact.id,{user:fact.user,subject:fact.subject,category:fact.category,text:fact.text,status});await refreshProductPage();await loadInlineMemory();toast(status==='confirmed'?'已确认，回复建议将参考此信息':'已忽略此条信息')}catch(e){toast(e.message)}finally{button.disabled=false}
}
function renderPeople(){
  const query=$('peopleSearch').value.toLocaleLowerCase();const root=$('knowledgePeople');root.replaceChildren();
  for(const session of state.sessions.filter(s=>(s.name+s.id+(s.note||'')).toLocaleLowerCase().includes(query))){
    const button=node('button',undefined,'person-row'+(session.id===product.user?' active':''));
    button.setAttribute('aria-pressed',String(session.id===product.user));button.append(node('span',session.name.slice(0,1),'avatar'));
    const body=node('span',undefined,'sessionbody');body.append(node('b',session.name),node('small',session.note|| (session.group?'群聊协作背景':'联系人知识库')));button.append(body);
    button.onclick=()=>{product.user=session.id;loadPersonKnowledge().catch(e=>toast(e.message))};root.append(button);
  }
  if(!state.sessions.length)root.append(node('p','先在工作台添加会话。','emptylist'));
}
async function loadPersonKnowledge(){
  product.user=product.user||state.user||state.sessions[0]?.id||null;renderPeople();
  const session=state.sessions.find(s=>s.id===product.user);$('knowledgeName').textContent=session?.name||'选择一位联系人';$('knowledgeNote').textContent=session?.note||(session?.group?'群聊知识描述共同背景和约定，不将所有成员视为同一人。':'双方的关键信息与关系经历，都可以在这里整理。');
  $('addFactBtn').disabled=$('learnBtn').disabled=!session;
  if(!session){$('personFacts').replaceChildren(emptyMemory('还没有联系人'));$('journalList').replaceChildren();metrics('knowledgeMetrics',[['工作台会话',0,'主动选择关注的人'],['已确认信息',0,'用于回复建议'],['待审阅信息',0,'由你确认事实']]);return}
  const request=++product.request,user=product.user;
  const [memory,journals,stats]=await Promise.all([api('knowledge?'+new URLSearchParams({user,include_rejected:true})),api('journals?'+new URLSearchParams({user,period:$('journalPeriod').value})),api('product')]);
  if(request!==product.request||user!==product.user)return;
  product.facts=memory.items;renderPersonFacts();renderJournals(journals.items);
  metrics('knowledgeMetrics',[['工作台会话',stats.people,'各自独立的知识库'],['已确认信息',memory.items.filter(f=>f.status==='confirmed').length,'当前联系人的关键信息'],['待审阅信息',memory.items.filter(f=>f.status==='pending').length,'核对原文后再确认']]);
}
function renderPersonFacts(){
  const filter=$('factFilter').value,items=product.facts.filter(f=>filter==='active'?f.status!=='rejected':f.status===filter);
  $('personFacts').replaceChildren(...(items.length?items.map(factCard):[emptyMemory('这个范围还没有知识条目')]));
}
async function loadSelfKnowledge(){
  const request=++product.request;const result=await api('knowledge?subject=self&include_rejected=true');if(request!==product.request)return;
  product.own=result.items;
  const active=result.items.filter(f=>f.status!=='rejected');$('selfFacts').replaceChildren(...(active.length?active.map(factCard):[emptyMemory('从自己的表达开始认识自己')]));
  metrics('selfMetrics',[['已确认的自己',active.filter(f=>f.status==='confirmed').length,'表达、习惯、目标与边界'],['等待你的确认',active.filter(f=>f.status==='pending').length,'AI 归纳不代表定论'],['知识来源',new Set(active.flatMap(f=>f.evidence.map(e=>e.user))).size,'当前工作台中的对话']]);
}
async function loadInlineMemory(){
  if(!state.user){$('inlineMemory').replaceChildren(node('p','选择会话后，查看这段关系的记忆。','hint'));return}
  const user=state.user,epoch=state.epoch;const context=await api('knowledge?user='+encodeURIComponent(user));
  if(user!==state.user||epoch!==state.epoch)return;
  const confirmed=context.items.filter(f=>f.status==='confirmed'),pending=context.items.filter(f=>f.status==='pending');
  $('inlineMemory').replaceChildren(node('p',confirmed.length+' 条已确认 · '+pending.length+' 条待审阅','hint'),...confirmed.slice(0,8).map(f=>{const card=node('div',undefined,'inline-fact');card.append(node('small',categoryNames[f.category]),node('p',f.text));return card}));
  if(!confirmed.length)$('inlineMemory').append(node('p','先整理对话或补充双方信息，再确认需要长期记住的内容。','hint'));
}
function renderJournals(items){
  const root=$('journalList');root.replaceChildren();
  for(const item of items){const card=node('details',undefined,'journal-card'),heading=node('summary',undefined);heading.append(node('span',periodNames[item.period]||'回顾','categorychip'),node('b',item.label),node('span',item.count+' 条消息','hint'));card.append(heading,node('pre',item.text));root.append(card)}
  if(!items.length)root.append(node('p','还没有这个周期的回顾。选择日期后点击“整理对话与知识”。','emptylist'));
}
let factEditing=null;
function openFact(subject,fact=null){
  const user=subject==='self'?'':product.user;if(subject==='other'&&!user)return toast('先选择联系人');
  factEditing={subject,user,...(fact?{id:fact.id,user:fact.user}: {})};
  $('factDialogTitle').textContent=fact?'编辑知识条目':subject==='self'?'补充我的信息':'补充联系人信息';
  $('factOwner').textContent=subject==='self'?'我的画像':state.sessions.find(s=>s.id===user)?.name||'';
  $('factCategory').value=fact?.category||'identity';$('factText').value=fact?.text||'';$('factStatus').value=fact?.status||'confirmed';$('factSaveStatus').textContent='';$('factSaveStatus').classList.remove('error');$('factDialog').showModal();
}
$('saveFactBtn').onclick=async()=>{const b=$('saveFactBtn');b.disabled=true;try{
  const body={user:factEditing.user,subject:factEditing.subject,category:$('factCategory').value,text:$('factText').value.trim(),status:$('factStatus').value};
  await api(factEditing.id?'knowledge/facts/'+factEditing.id:'knowledge',body);$('factDialog').close();await refreshProductPage();if(state.user)await loadInlineMemory();toast('知识条目已保存');
}catch(e){$('factSaveStatus').textContent=e.message;$('factSaveStatus').classList.add('error')}finally{b.disabled=false}};
async function runProductJob(endpoint,body,statusId,onDone){
  productStatus(statusId,'正在准备任务…');
  try{
    const task=await api(endpoint,body);
    while(true){
      const job=await api('product/jobs/'+task.id);productStatus(statusId,job.message,job.state==='error');
      if(job.state==='running'){const cancel=node('button','取消');cancel.onclick=()=>api('product/jobs/'+task.id+'/cancel',{}).catch(e=>toast(e.message));$(statusId).append(cancel)}
      if(job.state==='error')throw new Error(job.message);
      if(job.state==='cancelled')return;
      if(job.state==='done'){if(onDone)await onDone(job.result);return job.result}
      await new Promise(resolve=>setTimeout(resolve,900));
    }
  }catch(error){productStatus(statusId,error.message,true);throw error}
}
$('learnBtn').onclick=async()=>{if(!product.user)return;const b=$('learnBtn');b.disabled=true;try{await runProductJob('knowledge/run',{user:product.user,period:$('journalPeriod').value,anchor:$('journalAnchor').value||null},'learningStatus',async()=>{await loadPersonKnowledge();if(state.user)await loadInlineMemory()})}catch(e){toast(e.message)}finally{b.disabled=false}};
$('peopleSearch').oninput=renderPeople;$('factFilter').onchange=renderPersonFacts;
$('journalPeriod').onchange=()=>{$('journalAnchor').disabled=$('journalPeriod').value==='all';loadPersonKnowledge().catch(e=>toast(e.message))};
$('journalAnchor').value=beijingToday();$('addFactBtn').onclick=()=>openFact('other');$('addSelfFactBtn').onclick=()=>openFact('self');
$('openChatKnowledge').onclick=()=>{product.user=state.user;setProductView('knowledge')};
function eventCard(event){
  const card=node('button',undefined,'eventcard');card.append(node('span',event.title,'eventtitle'),node('small',event.start?formatEventTime(event):'时间待确认'));
  if(event.recurrence==='yearly')card.append(node('span','每年重复','categorychip'));
  if(event.status==='confirmed')card.append(node('small',event.sync_state==='synced'?'已同步到 CalDAV':event.sync_state==='conflict'?'远端冲突 · 未覆盖':event.sync_state==='error'?'同步失败 · 可重试':'本地日程 · 待同步','syncnote'));
  if(event.sync_error)card.append(node('small',event.sync_error,'syncerror'));card.onclick=()=>openEvent(event);return card;
}
function formatEventTime(event){const value=event.display_start||event.start;return event.all_day?value.slice(0,10)+' · 全天':value.slice(0,10)+' '+value.slice(11,16)}
async function loadCalendar(){
  const request=++product.request;const [month,all]=await Promise.all([api('calendar/events?month='+product.month),api('calendar/events')]);if(request!==product.request)return;
  product.events=all.items;$('calendarMonth').textContent=product.month.replace('-',' 年 ')+' 月';renderCalendarGrid(month.items);
  const pending=all.items.filter(e=>e.status==='draft'),confirmed=month.items.filter(e=>e.status==='confirmed'&&e.start);
  $('pendingEvents').replaceChildren(...(pending.length?pending.map(eventCard):[node('p','没有待确认的安排。','emptylist')]));
  $('monthEvents').replaceChildren(...(confirmed.length?confirmed.map(eventCard):[node('p','这个月还没有安排。','emptylist')]));
}
function renderCalendarGrid(events){
  const [year,month]=product.month.split('-').map(Number);const first=new Date(Date.UTC(year,month-1,1));const offset=(first.getUTCDay()+6)%7;
  const grid=$('calendarGrid');grid.replaceChildren();
  for(let index=0;index<42;index++){
    const day=new Date(Date.UTC(year,month-1,1-offset+index)),iso=day.toISOString().slice(0,10),cell=node('div',undefined,'calendar-cell'+(iso.slice(0,7)!==product.month?' outside':'')+(iso===beijingToday()?' today':''));
    const add=node('button',day.getUTCDate(),'calendar-date');add.setAttribute('aria-label',iso+'，添加日程');add.onclick=()=>openEvent(null,iso);cell.append(add);
    const begin=Date.parse(iso+'T00:00:00+08:00');
    const matches=events.filter(e=>e.status==='confirmed'&&e.start&&(e.all_day?(e.display_start||e.start).slice(0,10)<=iso&&(e.display_end||e.end).slice(0,10)>iso:Date.parse(e.display_start||e.start)<begin+86400000&&Date.parse(e.display_end||e.end)>begin));
    for(const event of matches.slice(0,3)){const b=node('button',event.title,'calendar-event');b.title=event.title+' · '+formatEventTime(event);b.onclick=()=>openEvent(event);cell.append(b)}
    if(matches.length>3){const more=node('button','+'+(matches.length-3)+' 项','calendar-more');more.onclick=()=>{const root=$('monthEvents');root.replaceChildren(...matches.map(eventCard));root.scrollIntoView({block:'nearest'})};cell.append(more)}
    grid.append(cell);
  }
}
function moveMonth(delta){const [year,month]=product.month.split('-').map(Number);product.month=new Date(Date.UTC(year,month-1+delta,1)).toISOString().slice(0,7);loadCalendar().catch(e=>toast(e.message))}
let eventEditing=null;
function eventInputs(allDay){const values=[$('eventStart').value,$('eventEnd').value];for(const id of ['eventStart','eventEnd'])$(id).type=allDay?'date':'datetime-local';values.forEach((value,index)=>$(index?'eventEnd':'eventStart').value=allDay?value.slice(0,10):value?(value.includes('T')?value.slice(0,16):value+'T09:00'):'');$('eventEndHint').textContent=allDay?'结束日期不包含在日程内；留空默认持续一天。':'使用北京时间；留空默认持续一小时。'}
function openEvent(event=null,day=null){
  eventEditing=event;$('eventDialogTitle').textContent=event?'查看与编辑日程':'新建日程';
  $('eventTitle').value=event?.title||'';$('eventAllDay').checked=event?.all_day??true;
  $('eventStart').value='';$('eventEnd').value='';eventInputs($('eventAllDay').checked);
  $('eventStart').value=event?.start?event.start.slice(0,event.all_day?10:16):day||beijingToday();
  $('eventEnd').value=event?.end?event.end.slice(0,event.all_day?10:16):'';
  $('eventRecurrence').value=event?.recurrence||'none';$('eventDescription').value=event?.description||'';$('eventLocation').value=event?.location||'';
  $('eventUser').replaceChildren(node('option','我的日程'));$('eventUser').firstElementChild.value='';for(const s of state.sessions){const o=node('option',s.name);o.value=s.id;$('eventUser').append(o)}
  $('eventUser').value=event?.user||'';$('eventUser').disabled=!!event;
  $('eventEvidence').replaceChildren(...(event?.evidence.length?[evidenceBlock(event.evidence)]:[]));
  $('ignoreEventBtn').hidden=!event||event.status!=='draft';$('saveDraftEventBtn').hidden=event?.status==='confirmed';$('eventSaveStatus').textContent='';$('eventSaveStatus').classList.remove('error');$('eventDialog').showModal();
}
async function saveEvent(status,button){button.disabled=true;try{
  const all_day=$('eventAllDay').checked;const body={user:eventEditing?.user||$('eventUser').value,title:$('eventTitle').value.trim(),start:$('eventStart').value,end:$('eventEnd').value,all_day,recurrence:$('eventRecurrence').value,description:$('eventDescription').value,location:$('eventLocation').value,status};
  if(!all_day){if(body.start)body.start+=':00+08:00';if(body.end)body.end+=':00+08:00'}
  await api(eventEditing?'calendar/events/'+eventEditing.id:'calendar/events',body);$('eventDialog').close();await loadCalendar();toast(status==='confirmed'?'日程已确认并加入本地日历':'候选状态已保存');
}catch(e){$('eventSaveStatus').textContent=e.message;$('eventSaveStatus').classList.add('error')}finally{button.disabled=false}}
$('eventAllDay').onchange=()=>eventInputs($('eventAllDay').checked);
$('confirmEventBtn').onclick=()=>saveEvent('confirmed',$('confirmEventBtn'));$('saveDraftEventBtn').onclick=()=>saveEvent('draft',$('saveDraftEventBtn'));$('ignoreEventBtn').onclick=()=>saveEvent('ignored',$('ignoreEventBtn'));
$('newEventBtn').onclick=()=>openEvent();$('prevMonth').onclick=()=>moveMonth(-1);$('nextMonth').onclick=()=>moveMonth(1);$('calendarToday').onclick=()=>{product.month=beijingToday().slice(0,7);loadCalendar().catch(e=>toast(e.message))};
$('calendarExportBtn').onclick=async()=>{try{await saveDownload(await downloadText('calendar/export'),'ics','对话日历.ics')}catch(e){toast(e.message)}};
$('calendarSyncBtn').onclick=async()=>{const b=$('calendarSyncBtn');b.disabled=true;try{await runProductJob('calendar/sync',{},'calendarJobStatus',loadCalendar)}catch(e){toast(e.message)}finally{b.disabled=false}};
async function showCalendarSettings(){
  const cfg=await api('calendar/settings');$('caldavUrl').value=cfg.url;$('caldavUsername').value=cfg.username;$('caldavPassword').value='';$('caldavPassword').placeholder=cfg.has_password?'已安全保存；留空保留原密码':'应用专用密码';$('caldavAutoSync').checked=cfg.auto_sync;
  const o=node('option',cfg.calendar_url?'已保存的目标日历':'先测试连接');o.value=cfg.calendar_url||'';$('caldavTarget').replaceChildren(o);$('caldavStatus').textContent='';$('caldavStatus').classList.remove('error');$('caldavDialog').showModal();
}
for(const id of ['caldavUrl','caldavUsername'])$(id).oninput=()=>{const option=node('option','请重新发现日历');option.value='';$('caldavTarget').replaceChildren(option)};
async function saveCalendarSettings(){return api('calendar/settings',{url:$('caldavUrl').value.trim(),username:$('caldavUsername').value.trim(),password:$('caldavPassword').value,calendar_url:$('caldavTarget').value,auto_sync:$('caldavAutoSync').checked})}
$('calendarSettingsBtn').onclick=()=>showCalendarSettings().catch(e=>toast(e.message));
$('discoverCalendarsBtn').onclick=async()=>{const b=$('discoverCalendarsBtn');b.disabled=true;try{
  await saveCalendarSettings();$('caldavPassword').value='';$('caldavStatus').textContent='正在发现日历…';
  const result=await api('calendar/discover',{}),previous=$('caldavTarget').value;$('caldavTarget').replaceChildren(...result.items.map(c=>{const o=node('option',c.name);o.value=c.url;return o}));if(result.items.some(c=>c.url===previous))$('caldavTarget').value=previous;
  $('caldavStatus').textContent='连接成功，找到 '+result.items.length+' 个日历。选择目标后保存。';
}catch(e){$('caldavStatus').textContent=e.message;$('caldavStatus').classList.add('error')}finally{b.disabled=false}};
$('saveCalendarSettings').onclick=async()=>{const b=$('saveCalendarSettings');b.disabled=true;try{await saveCalendarSettings();$('caldavPassword').value='';$('caldavStatus').textContent='连接设置已保存';toast('CalDAV 设置已保存')}catch(e){$('caldavStatus').textContent=e.message;$('caldavStatus').classList.add('error')}finally{b.disabled=false}};
async function loadAutomation(){
  const request=++product.request;const result=await api('automation');if(request!==product.request)return;
  for(const [id,key] of [['scheduleDay','day'],['scheduleWeek','week'],['scheduleMonth','month']])$(id).checked=result.settings[key];
  $('scheduleTime').value=result.settings.time;$('scheduleScope').value=result.settings.users.length?'selected':'all';
  $('scheduleUsers').replaceChildren(...state.sessions.map(s=>{const label=node('label',undefined,'checklabel'),check=node('input');check.type='checkbox';check.value=s.id;check.checked=result.settings.users.includes(s.id);label.append(check,node('span',s.name));return label}));$('scheduleUsers').hidden=$('scheduleScope').value!=='selected';
  $('backgroundEnabled').checked=result.background.enabled;$('backgroundEnabled').disabled=!result.background.supported;
  $('backgroundStatus').textContent=result.background.enabled?'Windows 后台检查已开启 · 每 15 分钟':'后台检查未开启 · 当前随应用运行';
  const root=$('automationRuns');root.replaceChildren(...result.runs.map(run=>{const row=node('div',undefined,'run-row');row.append(node('span',statusNames[run.state]||run.state,'statuschip '+run.state));const body=node('div',undefined,'run-body');body.append(node('b',(state.sessions.find(s=>s.id===run.user)?.name||'工作台')+' · '+(periodNames[run.period]||'整理')),node('p',run.message));row.append(body,node('time',new Date(run.started_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})));return row}));if(!result.runs.length)root.append(node('p','还没有执行记录。启用一个周期，或先手动整理对话。','emptylist'));
}
$('scheduleScope').onchange=()=>{$('scheduleUsers').hidden=$('scheduleScope').value!=='selected'};
async function saveSchedule(){const selected=$('scheduleScope').value==='selected',users=selected?[...$('scheduleUsers').querySelectorAll('input:checked')].map(e=>e.value):[];if(selected&&!users.length)throw new Error('请至少选择一个工作台会话');return api('automation',{day:$('scheduleDay').checked,week:$('scheduleWeek').checked,month:$('scheduleMonth').checked,time:$('scheduleTime').value,users})}
$('saveScheduleBtn').onclick=async()=>{const b=$('saveScheduleBtn');b.disabled=true;try{await saveSchedule();toast('周期整理设置已保存')}catch(e){toast(e.message)}finally{b.disabled=false}};
$('runAutomationBtn').onclick=async()=>{const b=$('runAutomationBtn');b.disabled=true;try{await saveSchedule();await runProductJob('automation/run',{},'automationJobStatus',loadAutomation)}catch(e){toast(e.message)}finally{b.disabled=false}};
$('backgroundEnabled').onchange=async()=>{const checkbox=$('backgroundEnabled');checkbox.disabled=true;try{const result=await api('automation/background',{enabled:checkbox.checked});checkbox.checked=result.enabled;$('backgroundStatus').textContent=result.enabled?'后台检查已开启 · 每 15 分钟':'后台检查已关闭'}catch(e){checkbox.checked=!checkbox.checked;$('backgroundStatus').textContent=e.message}finally{checkbox.disabled=false}};
for(const [id,view] of Object.entries({knowledgeNav:'knowledge',selfNav:'self',calendarNav:'calendar',automationNav:'automation'}))$(id).onclick=()=>setProductView(view);
document.body.dataset.view='chat';
