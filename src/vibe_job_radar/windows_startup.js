"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const controls = byId('startup-controls'), consent = byId('startup-consent');
  const message = byId('startup-message'), token = sessionStorage.getItem('radar-session') || '';
  let saved = null, pending = false, acceptedRevision = '';
  const revoke = () => {consent.checked = false; acceptedRevision = '';};
  async function call(action, body) {
    const options = {headers:{'X-Radar-Token':token}, cache:'no-store'};
    if (body !== undefined) {
      options.method = 'POST'; options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetch('/api/windows/startup/' + action, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '登录启动设置不可用。');
    return result;
  }
  function render(value) {
    if (saved?.revision !== value.revision) revoke();
    saved = value;
    byId('startup-status').textContent = value.message;
    byId('startup-paths').textContent = value.supported
      ? `程序：${value.executable}\n工作区：${value.workspace}\n启动应用名称：${value.value_name}` : '';
    controls.disabled = pending;
    consent.disabled = !value.can_enable;
    byId('startup-enable').disabled = pending || !value.can_enable;
    byId('startup-disable').disabled = pending || !value.can_disable;
  }
  async function refresh() {
    if (pending) return;
    try {render(await call('state'));}
    catch (error) {saved = null; controls.disabled = true; revoke(); byId('startup-status').textContent = error.message;}
  }
  async function change(action) {
    if (pending || !saved) return;
    if (action === 'enable' && (!consent.checked || acceptedRevision !== saved.revision)) {
      message.textContent = '请先核对程序和工作区，再勾选登录启动。'; return;
    }
    pending = true; controls.disabled = true; message.textContent = '';
    const body = {revision:saved.revision};
    if (action === 'enable') Object.assign(body, {consent:true, consent_version:'windows-portable-login-v1'});
    try {render(await call(action, body));}
    catch (error) {message.textContent = error.message;}
    finally {revoke(); pending = false; await refresh();}
  }
  consent.addEventListener('change', () => {acceptedRevision = consent.checked ? saved?.revision || '' : '';});
  byId('startup-enable').addEventListener('click', () => change('enable'));
  byId('startup-disable').addEventListener('click', () => change('disable'));
  byId('startup-refresh').addEventListener('click', () => {revoke(); refresh();});
  if (token) refresh();
  else byId('startup-status').textContent = '缺少本机会话令牌，请从启动器完整地址打开。';
})();
