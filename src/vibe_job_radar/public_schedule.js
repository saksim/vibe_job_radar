"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const form = byId('public-search'), controls = byId('schedule-controls');
  const consent = byId('schedule-consent'), message = byId('schedule-message');
  const localToken = sessionStorage.getItem('radar-session') || '';
  let saved = null, pending = false, acceptedQuery = '';
  const query = () => ({query:form.elements.query.value, region:form.elements.region.value,
    source_scope:[form.elements.source.value], limit:20});
  const revoke = () => {consent.checked = false; acceptedQuery = '';};
  async function call(action, body) {
    const options = {headers:{'X-Radar-Token':localToken}, cache:'no-store'};
    if (body !== undefined) {
      options.method = 'POST'; options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetch('/api/public/schedule/' + action, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '无法读取或保存本机计划。');
    return result;
  }
  function render(value) {
    if (saved && saved.revision !== value.revision) revoke();
    saved = value;
    controls.disabled = pending;
    byId('schedule-save').disabled = !value.available || pending;
    byId('schedule-stop').disabled = value.status === 'disabled' || pending;
    const next = value.next_due ? ` 下一次：${new Date(value.next_due * 1000).toLocaleString()}。` : '';
    byId('schedule-status').textContent = value.message + next;
    const q = value.query;
    const sourceLabels = q?.source_scope.map(key => Array.from(form.elements.source.options).find(option => option.value === key)?.textContent || '已保存来源').join('、');
    byId('schedule-query').textContent = q
      ? `已保存计划：${sourceLabels} · ${q.query} · 地区：${q.region || '不限'} · 每次最多${q.limit}条首屏结果。修改上方表单不会自动改动此计划。`
      : '尚未保存查询计划。';
    const entries = value.history.slice().reverse().map(item => {
      const li = document.createElement('li');
      const labels = {completed:'完成', failed:'失败', cancelled:'已停止', interrupted:'中断', unknown:'结果不确定'};
      li.textContent = `${new Date(item.finished_at * 1000).toLocaleString()} · ${labels[item.status] || '需核对'}${item.stale ? '（过期缓存）' : ''} `;
      if (/^[a-f0-9]{32}$/.test(item.report_id)) {
        const link = document.createElement('a');
        link.href = '/#report=' + item.report_id; link.textContent = '打开本次报告';
        link.addEventListener('click', event => {
          event.preventDefault(); location.hash = 'report=' + item.report_id; location.reload();
        });
        li.append(link);
      }
      return li;
    });
    byId('schedule-history').replaceChildren(...entries);
  }
  async function refresh() {
    if (pending) return;
    try {render(await call('state'));}
    catch (error) {saved = null; controls.disabled = true; revoke(); byId('schedule-status').textContent = error.message;}
  }
  async function change(action, body) {
    if (pending || !saved) return;
    pending = true; controls.disabled = true; message.textContent = '';
    try {render(await call(action, {...body, revision:saved.revision})); revoke();}
    catch (error) {message.textContent = error.message; revoke();}
    finally {pending = false; await refresh();}
  }
  for (const name of ['query', 'region', 'source']) {
    form.elements[name].addEventListener('input', revoke);
    form.elements[name].addEventListener('change', revoke);
  }
  consent.addEventListener('change', () => {acceptedQuery = consent.checked ? JSON.stringify(query()) : '';});
  byId('schedule-save').addEventListener('click', async () => {
    if (!saved?.available || !consent.checked || acceptedQuery !== JSON.stringify(query())) {
      message.textContent = '请先选择查询条件，再明确勾选24小时计划。'; return;
    }
    await change('configure', {consent:true, query:query()});
  });
  byId('schedule-stop').addEventListener('click', () => change('disable', {}));
  async function poll() {await refresh(); setTimeout(poll, 5000);}
  if (localToken) poll();
  else byId('schedule-status').textContent = '缺少本机会话令牌，请从启动器完整地址打开。';
})();
