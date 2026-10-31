// Upload settings stay in the local desktop; read credentials belong to the AI client.
const remoteSection=node('section',undefined,'remote-settings');
remoteSection.setAttribute('aria-labelledby','remoteTitle');
const remoteTitle=node('h3','服务端存储与同步');remoteTitle.id='remoteTitle';
const remoteDescription=node('p','将工作台内的聊天记录、备注、背景和已确认知识上传到你配置的服务端。服务端独立保存数据，客户端关闭后外部 AI 仍可读取。','hint');
function remoteField(id,title,type){const label=node('label',title),input=node('input');input.id=id;input.type=type;label.htmlFor=id;label.append(input);return {label,input}}
const remoteUrl=remoteField('remoteUrl','服务端地址','url');remoteUrl.input.placeholder='https://chat.example.com';remoteUrl.input.autocomplete='url';
const remoteToken=remoteField('remoteToken','上传 token','password');remoteToken.input.autocomplete='off';remoteToken.input.placeholder='填写服务端的 upload_token';
const remoteEnabledLabel=node('label',undefined,'checklabel'),remoteEnabled=node('input');remoteEnabled.type='checkbox';remoteEnabled.id='remoteEnabled';remoteEnabledLabel.htmlFor='remoteEnabled';remoteEnabledLabel.append(remoteEnabled,document.createTextNode('允许向此服务端自动上传当前账号的工作台记录'));
const remoteActions=node('div',undefined,'dialogactions'),remoteSave=node('button','保存并测试'),remoteRun=node('button','立即增量同步','primary'),remoteFull=node('button','重新上传全部历史');
remoteActions.append(remoteSave,remoteRun,remoteFull);
const remoteStatus=node('div',undefined,'statusbox');remoteStatus.id='remoteStatus';remoteStatus.setAttribute('role','status');remoteStatus.setAttribute('aria-live','polite');
const remoteHint=node('p','首次上传包含所选会话的全部本机历史，后续增量上传。移出会话的变更需同步成功后才会限制服务端读取；关闭上传会保留服务端已有数据。每个微信账号分别配置上传 token；读取 token 请只交给外部 AI。','hint');
remoteSection.append(remoteTitle,remoteDescription,remoteUrl.label,remoteToken.label,remoteEnabledLabel,remoteActions,remoteStatus,remoteHint);$('sourceDialog').append(remoteSection);
let remoteBusy=false;
function renderRemoteStatus(settings){const status=settings.status;remoteStatus.textContent=status.message+(status.last_success?' · 最近完成 '+new Date(status.last_success*1000).toLocaleString('zh-CN'):'');remoteStatus.classList.toggle('syncerror',status.state==='error');remoteRun.disabled=remoteFull.disabled=remoteBusy||!settings.enabled||status.state==='running'}
async function loadRemoteSettings(){const settings=await api('remote/settings');remoteUrl.input.value=settings.url;remoteToken.input.value='';remoteToken.input.placeholder=settings.has_token?'已安全保存；留空保留原 token':'填写服务端的 upload_token';remoteEnabled.checked=settings.enabled;renderRemoteStatus(settings)}
const showLocalSource=showSource;
showSource=async function(){await showLocalSource();await loadRemoteSettings()};
remoteSave.onclick=async()=>{if(remoteBusy)return;remoteBusy=true;remoteSave.disabled=true;remoteStatus.textContent='正在保存并测试服务端…';try{const settings=await api('remote/settings',{url:remoteUrl.input.value.trim(),token:remoteToken.input.value.trim(),enabled:remoteEnabled.checked});remoteToken.input.value='';renderRemoteStatus(settings);if(settings.url&&settings.has_token){const result=await api('remote/test',{});remoteStatus.textContent=result.message}else remoteStatus.textContent=settings.status.message;toast('服务端设置已保存')}catch(e){remoteStatus.textContent=e.message;remoteStatus.classList.add('syncerror')}finally{remoteBusy=false;remoteSave.disabled=false}};
async function runRemoteSync(full){remoteBusy=true;remoteRun.disabled=remoteFull.disabled=true;remoteStatus.textContent=full?'准备重新上传全部历史…':'准备增量同步…';try{await api('remote/sync',{full});toast('同步已启动，可在此查看上传进度')}catch(e){remoteStatus.textContent=e.message;remoteStatus.classList.add('syncerror')}finally{remoteBusy=false}}
remoteRun.onclick=()=>runRemoteSync(false);remoteFull.onclick=()=>runRemoteSync(true);
setInterval(async()=>{if(!$('sourceDialog').open||remoteBusy)return;try{renderRemoteStatus(await api('remote/settings'))}catch{}},2000);
