/* The desktop bridge is optional: browser previews never change OS settings. */
import {$, notify} from './api.js';
let saving = false;
const native = () => window.pywebview?.api;

function renderPreferences(value) {
  const startup = value.startup;
  $('autostart').checked = startup.enabled;
  $('autostart').disabled = !startup.supported || saving;
  $('autostart-hint').textContent = startup.blocked
    ? '已注册，但被 Windows 禁用。请在系统「启动应用」中启用。'
    : startup.enabled ? '登录 Windows 后自动启动，并安静驻留系统托盘。' : '已关闭。下次登录 Windows 时不会自动运行。';
  $('tray-status').textContent = value.tray_ready ? '系统托盘已就绪 · 双击图标打开窗口' : '托盘未就绪，关闭窗口将退出程序';
  $('hide-window').title = value.tray_ready ? '收起到系统托盘' : '退出客户端';
  $('hide-window').setAttribute('aria-label', $('hide-window').title);
}

export async function refreshPreferences() {
  if (!native()?.preferences || saving) return;
  try { renderPreferences(await native().preferences()); }
  catch { $('preferences-error').textContent = '无法读取系统设置，请重新打开此页重试。'; }
}

export async function openServer(url) {
  if (native()?.open_server) {
    if (!await native().open_server()) throw Error('无法打开默认浏览器');
  } else if (url) {
    window.open(url, '_blank', 'noopener');
  }
}

export async function copyText(value) {
  if (native()?.copy_text) await native().copy_text(value);
  else await navigator.clipboard.writeText(value);
}

async function activate() {
  if (!native()?.preferences) return;
  $('window-controls').hidden = false;
  $('preview-label').hidden = true;
  $('quit-app').disabled = false;
  await refreshPreferences();
}
$('minimize-window').onclick = () => native()?.minimize().catch(() => notify('无法最小化窗口'));
$('hide-window').onclick = () => native()?.hide_window().catch(() => notify('无法关闭窗口'));
$('quit-app').onclick = () => native()?.quit_app().catch(() => notify('无法退出客户端'));
$('autostart').onchange = async () => {
  if (saving || !native()?.update_autostart) return;
  const desired = $('autostart').checked;
  saving = true;
  $('autostart').disabled = true;
  $('preferences-error').textContent = '';
  try {
    renderPreferences(await native().update_autostart(desired));
    notify(desired ? '开机自启设置已保存' : '已关闭开机自启');
  } catch {
    $('autostart').checked = !desired;
    $('preferences-error').textContent = '设置未保存，请检查当前 Windows 用户权限后重试。';
  } finally {
    saving = false;
    await refreshPreferences();
  }
};
window.addEventListener('pywebviewready', activate);
window.addEventListener('focus', refreshPreferences);
activate();
