import {api, $, notify} from './api.js';
import {copyText, openServer, refreshPreferences} from './desktop.js';

const state = {current: null, initialized: false, refreshing: false, saving: false, selecting: false};
$('extract-image-key').onclick = async () => {
  const button = $('extract-image-key'); button.disabled = true; button.textContent = '正在提取…';
  try { await api('image-key', {}); notify('图片密钥已保存，图片将在后台继续同步'); }
  catch (error) { notify(error.message); }
  finally { button.disabled = false; button.textContent = '提取图片密钥'; }
};
const labels = {
  overview: ['运行概览', '连接状态与本次运行统计'],
  settings: ['连接设置', '连接服务端，选择本地微信账号'],
  mcp: ['MCP 接入', '将服务端能力连接到你的 AI 软件'],
  preferences: ['软件设置', '启动偏好与客户端信息'],
};

function page(name) {
  for (const item of document.querySelectorAll('[data-page]')) {
    const selected = item.dataset.page === name;
    item.classList.toggle('active', selected);
    if (selected) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  }
  for (const item of document.querySelectorAll('.page')) item.hidden = item.id !== 'page-' + name;
  $('page-title').textContent = labels[name][0];
  $('page-subtitle').textContent = labels[name][1];
  $('main-content').scrollTop = 0;
  if (name === 'preferences') refreshPreferences();
}

function badge(id, text, type = '') {
  $(id).textContent = text;
  $(id).className = 'badge ' + type;
}

function duration(seconds) {
  const totalMinutes = Math.floor(Math.max(0, seconds) / 60);
  $('uptime').replaceChildren(String(Math.floor(totalMinutes / 60)));
  const hours = document.createElement('small'); hours.textContent = '小时';
  const minutes = document.createElement('small'); minutes.textContent = '分';
  $('uptime').append(hours, String(totalMinutes % 60).padStart(2, '0'), minutes);
}

function renderAccounts(s) {
  const select = $('account-select');
  const selected = select.value;
  const signature = JSON.stringify(s.accounts);
  if (select.dataset.signature !== signature) {
    select.replaceChildren(new Option(s.accounts.length ? '请选择账号' : '尚未发现账号', ''));
    for (const account of s.accounts) select.add(new Option(account.account, account.path));
    select.dataset.signature = signature;
    select.value = selected || s.account_path;
  }
  if (!select.value && s.account_path) select.value = s.account_path;
  $('account-hint').textContent = !s.configured ? '完成服务端连接后检测账号。'
    : !s.accounts.length ? '请先登录微信，然后刷新账号。'
    : s.accounts.length > 1 ? '发现多个账号，请选择需要解析和上传的账号。' : '已发现本机账号，将自动加载解析器。';
  $('choose-account').disabled = state.selecting || !s.configured || !select.value;
}

function renderMcp() {
  const key = $('mcp-key').value.trim();
  const url = state.current?.url;
  // Avoid resetting the selection in the readonly preview on every status poll.
  const config = url && key ? JSON.stringify({mcpServers: {'wechat-memory': {url: url + '/mcp', headers: {Authorization: 'Bearer ' + key}}}}, null, 2) : '';
  if ($('mcp-config').value !== config) $('mcp-config').value = config;
  $('copy-mcp').disabled = !config;
  $('mcp-state').textContent = !url ? '请先完成服务端连接' : !key ? '填入 MCP Key 后可复制' : '配置仅包含服务端地址与 MCP Key';
}

function render(s) {
  state.current = s;
  if (!state.initialized) {
    $('url').value = s.url;
    $('app-key').value = s.app_key;
    $('app-secret').placeholder = s.has_secret ? '已保存，留空保留原密钥' : '输入客户端密钥';
    $('consent').checked = s.configured;
    state.initialized = true;
    if (!s.configured) page('settings');
  }
  duration(s.uptime_seconds);
  $('changed').textContent = s.changed_messages.toLocaleString('zh-CN');
  $('last-success').textContent = s.sync.last_success ? new Date(s.sync.last_success * 1000).toLocaleString('zh-CN', {hour12: false}) : '尚未同步';
  $('server-state').textContent = s.server_message;
  $('parser-state').textContent = s.parser_message;
  $('sync-state').textContent = s.sync.message || '等待解析器加载';
  const priority = s.sync.priority_count || 0;
  const lastPriority = s.sync.priority_last_success ? new Date(s.sync.priority_last_success * 1000).toLocaleTimeString('zh-CN', {hour12:false}) : '';
  $('priority-state').textContent = s.sync.priority_available === false ? '重点列表获取失败，正在重试' :
    s.sync.priority_available !== true ? '重点同步状态正在检查' :
    priority ? `重点会话 ${priority} 个 · 最近检查 ${lastPriority || '进行中'}` : '当前没有本机可同步的重点会话';
  badge('server-badge', s.server_state === 'ready' ? '已连接' : s.server_state === 'error' ? '连接异常' : '等待连接', s.server_state === 'ready' ? 'good' : s.server_state === 'error' ? 'bad' : '');
  const parserReady = s.parser_state === 'ready' && s.account_path && s.accounts.some(a => a.path === s.account_path);
  badge('parser-badge', parserReady ? '已加载' : s.parser_state === 'error' ? '加载失败' : '等待加载', parserReady ? 'good' : s.parser_state === 'error' ? 'bad' : '');
  const syncLabels = {done: ['已同步', 'good'], running: ['同步中', 'working'], error: ['同步异常', 'bad']};
  badge('sync-badge', ...(syncLabels[s.sync.state] || ['等待同步', '']));
  const failed = s.server_state === 'error' || s.parser_state === 'error' || s.sync.state === 'error';
  const healthy = s.server_state === 'ready' && parserReady && ['done', 'running'].includes(s.sync.state);
  const headline = failed ? '同步需要处理' : healthy ? '一切就绪，正在守候新消息' : !s.configured ? '连接服务端，开始同步' : '正在准备同步';
  $('headline').textContent = headline;
  $('headline-detail').textContent = healthy ? '仅同步群聊与通讯录好友，可收起窗口继续运行。' : s.server_state === 'error' ? s.server_message : s.parser_message;
  badge('overall', failed ? '需关注' : healthy ? '运行中' : '待就绪', failed ? 'bad' : healthy ? 'good' : '');
  $('footer-state').textContent = failed ? '连接或同步异常' : healthy ? '后台同步已就绪' : !s.configured ? '等待配置' : '等待解析器就绪';
  document.querySelector('.status-dot').className = 'status-dot ' + (failed ? 'bad' : healthy ? 'good' : '');
  $('footer-host').textContent = s.url ? new URL(s.url).host : '尚未配置服务端';
  $('footer-host').title = s.url || '';
  $('open-server').disabled = !s.configured;
  renderAccounts(s);
  renderMcp();
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    render(await api('status'));
  } catch {
    $('footer-state').textContent = '客户端连接中断 · 正在重试';
    document.querySelector('.status-dot').className = 'status-dot bad';
    $('headline').textContent = '暂时无法获取运行状态';
    $('headline-detail').textContent = '显示的是上次获取的数据，连接恢复后将自动更新。';
    badge('overall', '状态未更新', 'bad');
  } finally { state.refreshing = false; }
}

const fieldIds = ['url', 'app-key', 'app-secret', 'consent'];
function clearErrors() {
  $('settings-error').hidden = true;
  $('settings-error').replaceChildren();
  for (const id of fieldIds) { $(id).removeAttribute('aria-invalid'); $(id + '-error').textContent = ''; }
}
function formError(message, fields = {}) {
  const summary = $('settings-error');
  summary.replaceChildren(message);
  const list = document.createElement('ul');
  for (const [id, message] of Object.entries(fields)) {
    $(id).setAttribute('aria-invalid', 'true');
    $(id + '-error').textContent = message;
    const link = document.createElement('a'); link.href = '#' + id; link.textContent = message;
    link.onclick = event => { event.preventDefault(); $(id).focus(); };
    const item = document.createElement('li'); item.append(link); list.append(item);
  }
  if (list.children.length) summary.append(list);
  summary.hidden = false;
  summary.focus();
}
function validate() {
  const fields = {};
  try { const url = new URL($('url').value.trim()); if (!['http:', 'https:'].includes(url.protocol)) throw Error(); }
  catch { fields.url = '请填写完整的服务端地址'; }
  if (!$('app-key').value.trim().startsWith('app_')) fields['app-key'] = '请填写以 app_ 开头的 appKey';
  const s = state.current;
  const unchanged = s?.has_secret && $('url').value.trim().replace(/\/+$/, '') === s.url && $('app-key').value.trim() === s.app_key;
  if ($('app-secret').value.length < 32 && !(!$('app-secret').value && unchanged)) fields['app-secret'] = '请填写完整的 appSecret';
  if (!$('consent').checked) fields.consent = '请先确认解析与上传授权';
  if (Object.keys(fields).length) { formError('请检查以下内容：', fields); return false; }
  return true;
}

for (const item of document.querySelectorAll('[data-page]')) item.onclick = () => page(item.dataset.page);
$('overview-settings').onclick = () => page('settings');
$('account-select').onchange = () => { $('choose-account').disabled = state.selecting || !state.current?.configured || !$('account-select').value; };
$('mcp-key').oninput = renderMcp;
$('refresh-accounts').onclick = async () => {
  $('refresh-accounts').disabled = true;
  try { await refresh(); } finally { $('refresh-accounts').disabled = false; }
};
$('settings-form').onsubmit = async event => {
  event.preventDefault();
  if (state.saving) return;
  clearErrors();
  if (!validate()) return;
  state.saving = true;
  $('save-settings').disabled = true;
  $('save-settings').textContent = '正在检测连接…';
  try {
    const s = await api('settings', {url: $('url').value.trim(), app_key: $('app-key').value.trim(), app_secret: $('app-secret').value, consent: $('consent').checked});
    $('url').value = s.url;
    $('app-key').value = s.app_key;
    $('app-secret').value = '';
    $('app-secret').placeholder = '已保存，留空保留原密钥';
    render(s);
    notify('服务端连接成功，正在检查微信账号');
    page('overview');
  } catch (error) { formError(error.message); }
  finally { state.saving = false; $('save-settings').disabled = false; $('save-settings').textContent = '检测连接并保存'; }
};
$('choose-account').onclick = async () => {
  if (state.selecting) return;
  state.selecting = true;
  $('choose-account').disabled = true;
  $('account-error').textContent = '';
  try { render(await api('account', {path: $('account-select').value})); page('overview'); }
  catch (error) { $('account-error').textContent = error.message; $('account-error').focus(); }
  finally { state.selecting = false; $('choose-account').disabled = !state.current?.configured || !$('account-select').value; }
};
$('open-server').onclick = () => openServer(state.current?.url).catch(error => notify(error.message));
$('copy-mcp').onclick = async () => {
  if (!$('mcp-config').value) return;
  try { await copyText($('mcp-config').value); notify('MCP 配置已复制'); }
  catch { notify('复制失败，请手动选取配置'); $('mcp-config').focus(); $('mcp-config').select(); }
};
// Pause UI polling when hidden; the Python collector continues independently.
async function poll() { if (!document.hidden) await refresh(); setTimeout(poll, 3000); }
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
poll();
