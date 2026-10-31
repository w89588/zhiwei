/* Client setup uses reviewed plans; settings and credentials never reach this UI. */
const clientSetup = {selected:new Set(), token:null, busy:false, generation:0};
const clientStates = {missing:'待配置', configured:'配置一致', conflict:'同名内容不同', invalid:'需修正配置'};
function invalidateClientPlan(){
  clientSetup.generation++;clientSetup.token=null;
  $('clientPreview').hidden=true;$('clientPreview').replaceChildren();
  $('applyClients').disabled=true;
  $('previewClients').disabled=clientSetup.busy||!clientSetup.selected.size;
}
function clientBusy(busy){
  clientSetup.busy=busy;
  for(const id of ['rescanClients','installClientSkill','replaceClientConfig'])$(id).disabled=busy;
  $('clientList').inert=busy;
  $('previewClients').disabled=busy||!clientSetup.selected.size;
  $('applyClients').disabled=busy||!clientSetup.token;
}
async function loadAiClients(preserveResult=false){
  invalidateClientPlan();clientSetup.selected.clear();clientBusy(true);
  $('clientScanStatus').textContent='正在检测本机配置目录与命令行入口…';
  if(!preserveResult){$('clientSetupStatus').textContent='';$('clientSetupStatus').classList.remove('error')}
  try{
    const result=await api('integration/clients'),root=$('clientList');root.replaceChildren();
    const detected=result.items.filter(c=>c.detected).length;
    for(const client of [...result.items].sort((a,b)=>Number(b.detected)-Number(a.detected))){
      const row=node('label',undefined,'clientrow'+(!client.detected?' undetected':''));
      const check=node('input');check.type='checkbox';check.disabled=!client.detected||!!client.error;
      check.setAttribute('aria-label','配置 '+client.name);
      check.onchange=()=>{check.checked?clientSetup.selected.add(client.id):clientSetup.selected.delete(client.id);invalidateClientPlan();$('clientSetupStatus').textContent=''};
      const content=node('div',undefined,'clientcontent'),head=node('div',undefined,'clientheading');
      head.append(node('strong',client.name),node('span',client.detected?clientStates[client.state]:'未检测到','clientbadge '+client.state));
      content.append(head,node('small',client.error||client.evidence||'可展开下方手动配置'),node('code',client.path,'clientpath'));
      row.append(check,content);root.append(row);
    }
    $('clientScanStatus').textContent='发现 '+detected+' 个客户端的安装或配置线索 · 配置状态尚未验证连接';
  }catch(e){$('clientScanStatus').textContent='检测失败，请重新检测';$('clientList').replaceChildren();$('clientSetupStatus').textContent=e.message;$('clientSetupStatus').classList.add('error')}
  finally{clientBusy(false)}
}
$('rescanClients').onclick=()=>loadAiClients();
for(const id of ['installClientSkill','replaceClientConfig'])$(id).onchange=invalidateClientPlan;
$('previewClients').onclick=async()=>{
  invalidateClientPlan();clientBusy(true);const generation=clientSetup.generation;
  $('clientSetupStatus').classList.remove('error');$('clientSetupStatus').textContent='正在生成配置预览…';
  try{
    const result=await api('integration/clients/preview',{clients:[...clientSetup.selected],install_skill:$('installClientSkill').checked,replace_existing:$('replaceClientConfig').checked});
    if(generation!==clientSetup.generation)return;
    clientSetup.token=result.token;const root=$('clientPreview');root.replaceChildren();root.append(node('h3','即将写入'));
    const actions={create:'新建',update:'更新并备份',unchanged:'已一致，无需写入'};
    for(const item of result.items){
      const card=node('div',undefined,'clientplan');card.append(node('strong',item.name+(item.conflict?' · 将替换同名内容':'')));
      for(const file of item.files){const line=node('div',undefined,'clientplanfile');line.append(node('span',file.kind+' · '+actions[file.action]),node('code',file.path));card.append(line)}
      const details=node('details'),summary=node('summary','查看此客户端的 MCP 配置');details.append(summary,node('pre',JSON.stringify(item.config,null,2)));card.append(details,node('p',item.note,'hint'));root.append(card);
    }
    root.hidden=false;$('clientSetupStatus').textContent='已生成预览，尚未修改文件。确认路径后点击“写入配置”。';
    $('applyClients').textContent='写入配置';
  }catch(e){$('clientSetupStatus').textContent=e.message;$('clientSetupStatus').classList.add('error')}
  finally{clientBusy(false)}
};
$('applyClients').onclick=async()=>{
  const token=clientSetup.token;if(!token)return;clientSetup.token=null;clientBusy(true);
  $('clientSetupStatus').classList.remove('error');$('clientSetupStatus').textContent='正在备份并写入配置…';
  try{
    const result=await api('integration/clients/apply',{token}),root=$('clientSetupStatus');root.replaceChildren();
    for(const item of result.items){
      root.append(node('strong',item.name+' · '+(item.ok?(item.changed?'已写入':'已是最新'):'写入未完成')),node('p',item.note));
      for(const path of item.written||[])root.append(node('code','已写入：'+path));
      for(const path of item.backups||[])root.append(node('code','备份：'+path));
    }
    root.classList.toggle('error',result.items.some(item=>!item.ok));
    root.append(node('p',$('integrationEnabled').checked?'请重新加载客户端，并调用 wechat_status 验证连接。':'接入开关仍关闭。开启上方读取权限、重新加载客户端后，再验证连接。'));
    await loadAiClients(true);
  }catch(e){$('clientSetupStatus').textContent=e.message;$('clientSetupStatus').classList.add('error')}
  finally{clientBusy(false)}
};
