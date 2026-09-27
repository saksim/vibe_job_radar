/* Shared same-origin workspace preferences. No credentials or network probes. */
(() => {
  'use strict';
  const panel = document.getElementById('network-preferences');
  if (!panel) return;
  const toggle = panel.querySelector('input');
  const button = panel.querySelector('button');
  const message = panel.querySelector('[role=status]');
  message.id = 'network-dns-status';
  // A fieldset preserves disabled state when other page renderers change buttons.
  const controls = document.createElement('fieldset');
  controls.disabled = true;
  controls.style.cssText = 'border:0;padding:0;margin:0;min-width:0';
  const details = panel.querySelector('details');
  for (const child of [...details.children]) if (child.tagName !== 'SUMMARY') controls.append(child);
  details.append(controls);
  const proxy = document.createElement('fieldset');
  proxy.style.cssText = 'min-width:0;max-width:100%';
  proxy.innerHTML = `<legend>本工作区的代理</legend>
    <p>自动模式沿用应用或系统配置。固定入口仅支持匿名本机代理；更换后请停止并重开已有采集会话。</p>
    <label for="workspace-proxy-mode">连接方式</label>
    <select id="workspace-proxy-mode"><option value="auto">自动发现</option><option value="http">固定本机 HTTP</option><option value="socks5">固定本机 SOCKS5</option></select>
    <label for="workspace-proxy-endpoint">已配置的代理地址和端口</label>
    <input id="workspace-proxy-endpoint" type="text" autocomplete="off" spellcheck="false" style="max-width:100%;box-sizing:border-box" placeholder="填写你的本机代理入口，不含账号密码" disabled>
    <label><input id="workspace-proxy-consent" type="checkbox" disabled>确认仅在此工作区使用上述匿名本机代理</label>
    <p>认证代理继续使用应用专用配置；检测到冲突时停止。不会将凭据转交给这里的新地址。保存不会访问代理或招聘网站。</p>
    <button id="save-workspace-proxy" type="button">保存工作区代理</button>
    <p id="workspace-proxy-status" role="status" aria-live="polite">正在读取工作区代理。</p>`;
  controls.append(proxy);
  const mode = proxy.querySelector('#workspace-proxy-mode');
  const endpoint = proxy.querySelector('#workspace-proxy-endpoint');
  const consent = proxy.querySelector('#workspace-proxy-consent');
  const proxyButton = proxy.querySelector('#save-workspace-proxy');
  const proxyMessage = proxy.querySelector('#workspace-proxy-status');
  let revision = null, busy = false;
  function selection() {
    endpoint.disabled = consent.disabled = mode.value === 'auto';
    consent.checked = false;
  }
  mode.addEventListener('change', selection);
  endpoint.addEventListener('input', () => { consent.checked = false; });
  async function call(path, data) {
    const token = sessionStorage.getItem('radar-session') || '';
    const response = await fetch(path, {method: data ? 'POST' : 'GET', cache: 'no-store',
      headers: {'X-Radar-Token': token, ...(data ? {'Content-Type': 'application/json'} : {})},
      body: data ? JSON.stringify(data) : undefined});
    const value = await response.json();
    if (!response.ok) throw Error(value.error || '无法保存网络偏好');
    return value;
  }
  function render(value) {
    toggle.checked = value.mode === 'fake_ip_doh';
    revision = value.revision;
    mode.value = value.proxy_mode || 'auto';
    endpoint.value = value.proxy_endpoint || '';
    selection();
    proxyMessage.textContent = value.policy?.code === 'workspace_proxy_environment_conflict'
      ? '工作区代理与应用专用环境配置冲突，联网已停止。请清除冲突或改用自动模式。'
      : (mode.value === 'auto' ? '采用自动发现；尚未进行网络测试。' : '已保存 '+endpoint.value+'，仅对本工作区生效；尚未进行网络测试。');
  }
  async function save(path, data, output) {
    if (revision === null || busy) return;
    busy = true;controls.disabled = true;
    try {
      const value = await call(path, {...data, revision});
      render(value);output.textContent = value.message;
    } catch (error) { output.textContent = error.message; }
    finally { busy = false;controls.disabled = false; }
  }
  button.addEventListener('click', () => save('/api/network/preferences',
    {mode: toggle.checked ? 'fake_ip_doh' : 'system', consent: toggle.checked}, message));
  proxyButton.addEventListener('click', () => save('/api/network/proxy',
    {mode: mode.value, endpoint: mode.value === 'auto' ? '' : endpoint.value,
      consent: mode.value === 'auto' ? false : consent.checked}, proxyMessage));
  call('/api/network/state').then(value => {
    render(value);
    message.textContent = toggle.checked ? '已同意仅在映射地址时采用加密解析。尚未进行网络测试。' : '未开启额外解析；普通公网DNS不受影响。';
    button.disabled = false;controls.disabled = false;
  }).catch(error => { message.textContent = error.message;proxyMessage.textContent = '设置未就绪；请核对原配置后重新载入。'; });
})();
