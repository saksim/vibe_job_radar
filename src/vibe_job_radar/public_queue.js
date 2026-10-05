"use strict";
(() => {
  const byId = id => document.getElementById(id), form = byId('public-search');
  const controls = byId('queue-controls'), message = byId('queue-message');
  const consent = byId('queue-add-consent'), resumeConsent = byId('queue-resume-consent');
  const token = sessionStorage.getItem('radar-session') || '';
  let saved = null, pending = false, acceptedQuery = '', acceptedRevision = null;
  const query = () => ({query:form.elements.query.value, region:form.elements.region.value,
    source_scope:[form.elements.source.value], limit:20});
  function revoke() {consent.checked = resumeConsent.checked = false; acceptedQuery = ''; acceptedRevision = null;}
  function describe(q) {
    const source = Array.from(form.elements.source.options).find(option => option.value === q.source_scope[0]);
    return `${source?.textContent || '已保存来源'} · ${q.query} · 地区：${q.region || '不限'}`;
  }
  async function call(action, body) {
    const options = {headers:{'X-Radar-Token':token}, cache:'no-store'};
    if (body !== undefined) {
      options.method = 'POST'; options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body);
    }
    const response = await fetch('/api/public/queue/' + action, options), value = await response.json();
    if (!response.ok) throw new Error(value.error || '无法读取或保存待办。');
    return value;
  }
  function render(value) {
    if (saved && saved.revision !== value.revision) revoke();
    saved = value; controls.disabled = pending || value.code === 'storage_error';
    byId('queue-add').disabled = pending || !value.available || value.items.length >= value.capacity;
    byId('queue-pause').disabled = pending || !value.items.length || value.status === 'paused';
    byId('queue-resume').disabled = pending || !value.available || value.status !== 'paused' || !value.items.length
      || value.items.some(item => item.phase !== 'pending');
    const next = value.status === 'queued' && value.next_due > Date.now()/1000
      ? ` 最早下一次：${new Date(value.next_due*1000).toLocaleString()}。` : '';
    byId('queue-status').textContent = value.message + next;
    byId('queue-items').replaceChildren(...value.items.map(item => {
      const li = document.createElement('li');
      li.textContent = `${describe(item.query)} · ${{pending:'待运行',dispatching:'正在启动',running:'正在查询'}[item.phase]} `;
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'secondary';
      remove.textContent = '移除'; remove.disabled = pending || item.phase !== 'pending';
      remove.addEventListener('click', () => change('remove', {id:item.id})); li.append(remove); return li;
    }));
    byId('queue-history').replaceChildren(...value.history.slice().reverse().map(item => {
      const li = document.createElement('li');
      const status = {completed:'完成',failed:'失败',cancelled:'已停止',interrupted:'结果不确定',expired:'许可已过期'};
      li.textContent = `${describe(item.query)} · ${status[item.status] || '需核对'}${item.stale ? '（过期缓存）' : ''} `;
      if (/^[a-f0-9]{32}$/.test(item.report_id)) {
        const link = document.createElement('a'); link.href = '/#report=' + item.report_id; link.textContent = '打开本次报告';
        link.addEventListener('click', event => {event.preventDefault(); location.hash = 'report=' + item.report_id; location.reload();});
        li.append(link);
      }
      return li;
    }));
  }
  async function refresh() {
    if (pending) return;
    try {render(await call('state'));}
    catch (error) {saved = null; controls.disabled = true; revoke(); byId('queue-status').textContent = error.message;}
  }
  async function change(action, body) {
    if (pending || !saved) return;
    pending = true; controls.disabled = true; message.textContent = '';
    try {render(await call(action, {...body, revision:saved.revision})); revoke();}
    catch (error) {message.textContent = error.message; revoke();}
    finally {pending = false; await refresh();}
  }
  for (const name of ['query','region','source']) {
    form.elements[name].addEventListener('input', revoke); form.elements[name].addEventListener('change', revoke);
  }
  consent.addEventListener('change', () => {acceptedQuery = consent.checked ? JSON.stringify(query()) : '';});
  resumeConsent.addEventListener('change', () => {acceptedRevision = resumeConsent.checked ? saved?.revision : null;});
  byId('queue-add').addEventListener('click', () => {
    if (!saved?.available || !consent.checked || acceptedQuery !== JSON.stringify(query())) {
      message.textContent = '请先核对上方查询，再勾选加入待办的许可。'; return;
    }
    if (!form.elements.query.reportValidity() || !form.elements.source.reportValidity()) return;
    change('enqueue', {consent:true, query:query()});
  });
  byId('queue-pause').addEventListener('click', () => change('pause', {}));
  byId('queue-resume').addEventListener('click', () => {
    if (!resumeConsent.checked || acceptedRevision !== saved?.revision) {
      message.textContent = '请先核对当前剩余待办，再勾选继续许可。'; return;
    }
    change('resume', {consent:true});
  });
  async function poll() {await refresh(); setTimeout(poll, 5000);}
  if (token) poll(); else byId('queue-status').textContent = '缺少本机会话令牌，请从启动器完整地址打开。';
})();
