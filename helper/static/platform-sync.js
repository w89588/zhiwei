// V2 registration and upload consent. Secret values are stored by the desktop via DPAPI.
const platformSection=node('section',undefined,'remote-settings');
platformSection.setAttribute('aria-labelledby','platformSyncTitle');
const platformTitle=node('h3','连接 2.0 记忆平台');platformTitle.id='platformSyncTitle';
const platformDescription=node('p','在服务端创建客户端后，填写 appKey 和 appSecret 绑定当前电脑。通讯录与聊天记录按账户加密上传；监听与自动记忆在服务端管理。','hint');
const platformUrl=remoteField('platformUrl','服务端地址','url');platformUrl.input.placeholder='http://127.0.0.1:8787';
const platformKey=remoteField('platformKey','appKey','text');platformKey.input.autocomplete='off';
const platformSecret=remoteField('platformSecret','appSecret','password');platformSecret.input.autocomplete='off';platformSecret.input.placeholder='只在创建客户端时展示的密钥';
const platformScopeLabel=node('label','当前微信账号的上传范围'),platformScope=node('select');platformScope.id='platformScope';platformScopeLabel.htmlFor='platformScope';
for(const [v,t]of [['selected','工作台所选群聊、通讯录好友及对应资料'],['all','本机此微信账号的群聊、通讯录好友及对应资料']]){const o=node('option',t);o.value=v;platformScope.append(o)}platformScopeLabel.append(platformScope);
const platformConsentLabel=node('label',undefined,'checklabel'),platformConsent=node('input');platformConsent.type='checkbox';platformConsent.id='platformConsent';platformConsentLabel.htmlFor='platformConsent';platformConsentLabel.append(platformConsent,document.createTextNode('授权按以上范围解析当前微信账号并持续加密上传到此平台'));
const platformActions=node('div',undefined,'dialogactions'),platformSave=node('button','保存并注册','primary'),platformSync=node('button','立即同步'),platformFull=node('button','重传全部历史');platformActions.append(platformSave,platformSync,platformFull);
const platformStatus=node('div',undefined,'statusbox');platformStatus.setAttribute('role','status');platformStatus.setAttribute('aria-live','polite');
const platformHint=node('p','注册只绑定电脑，不会向微信发送消息。改变上传范围会影响后续采集；已上传数据与服务端监听须在服务端单独管理。每个已授权微信账号都会在后台独立同步。','hint');
platformSection.append(platformTitle,platformDescription,platformUrl.label,platformKey.label,platformSecret.label,platformScopeLabel,platformConsentLabel,platformActions,platformStatus,platformHint);
const sourceHead=$('sourceDialog').querySelector('.dialoghead');if(sourceHead)sourceHead.after(platformSection);else $('sourceDialog').prepend(platformSection);
let platformBusy=false;
function renderPlatformStatus(settings){const s=settings.status;platformStatus.textContent=s.message+(s.last_success?' · '+new Date(s.last_success*1000).toLocaleString('zh-CN'):'');platformStatus.classList.toggle('syncerror',s.state==='error');platformSync.disabled=platformFull.disabled=platformBusy||!settings.enabled||s.state==='running'}
async function loadPlatformSettings(){const s=await api('platform/settings');platformUrl.input.value=s.url;platformKey.input.value=s.app_key;platformSecret.input.value='';platformSecret.input.placeholder=s.has_secret?'已安全保存；留空保留原密钥':'填写客户端 appSecret';platformScope.value=s.scope;platformConsent.checked=s.enabled;renderPlatformStatus(s)}
const showLegacySource=showSource;showSource=async function(){await showLegacySource();await loadPlatformSettings()};
platformSave.onclick=async()=>{if(platformBusy)return;platformBusy=true;platformSave.disabled=true;platformStatus.textContent='正在保存与注册…';try{const s=await api('platform/settings',{url:platformUrl.input.value.trim(),app_key:platformKey.input.value.trim(),app_secret:platformSecret.input.value.trim(),enabled:platformConsent.checked,scope:platformScope.value});platformSecret.input.value='';renderPlatformStatus(s);if(s.enabled){const r=await api('platform/test',{});platformStatus.textContent=r.message;await api('platform/sync',{})}toast('平台配置已保存')}catch(e){platformStatus.textContent=e.message;platformStatus.classList.add('syncerror')}finally{platformBusy=false;platformSave.disabled=false}};
async function runPlatform(full){platformBusy=true;platformSync.disabled=platformFull.disabled=true;try{await api('platform/sync',{full});platformStatus.textContent='同步已启动，可稍后查看进度'}catch(e){platformStatus.textContent=e.message}finally{platformBusy=false}}
platformSync.onclick=()=>runPlatform(false);platformFull.onclick=()=>runPlatform(true);
setInterval(async()=>{if(!$('sourceDialog').open||platformBusy)return;try{renderPlatformStatus(await api('platform/settings'))}catch{}},2500);
