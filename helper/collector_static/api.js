/* Local API transport. No native OS actions are exposed by this HTTP surface. */
export async function api(path, body) {
  const response = await fetch('/api/' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'X-Helper-Token': window.HELPER_TOKEN, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let message = '请求失败，请稍后重试';
    try { message = (await response.json()).detail || message; } catch { /* Keep the readable fallback. */ }
    throw new Error(typeof message === 'string' ? message : '输入内容无效');
  }
  return response.json();
}

export const $ = id => document.getElementById(id);
let noticeTimer;
export function notify(message) {
  clearTimeout(noticeTimer);
  $('notice-text').textContent = message;
  $('notice').hidden = false;
  noticeTimer = setTimeout(() => { $('notice').hidden = true; }, 7000);
}
$('dismiss-notice').onclick = () => { $('notice').hidden = true; };
