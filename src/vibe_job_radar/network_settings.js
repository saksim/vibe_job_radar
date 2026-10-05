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
    <p>自动模式沿用应用或系统配置。固定入口支持匿名本机代理，或已授权给此来宾的宿主机代理；更换后请停止并重开已有采集会话。</p>
    <label for="workspace-proxy-mode">连接方式</label>
    <select id="workspace-proxy-mode"><option value="auto">自动发现</option><option value="http">固定本机 HTTP</option><option value="socks5">固定本机 SOCKS5</option><option value="vm_http">宿主机 HTTP</option><option value="vm_socks5">宿主机 SOCKS5</option><option value="pac" disabled>已导入的 PAC</option><option value="system_pac" disabled>已许可的系统 PAC</option></select>
    <label for="workspace-proxy-endpoint">已配置的代理地址和端口</label>
    <input id="workspace-proxy-endpoint" type="text" autocomplete="off" spellcheck="false" style="max-width:100%;box-sizing:border-box" placeholder="填写你的本机代理入口，不含账号密码" disabled>
    <p id="workspace-proxy-scope"></p>
    <label><input id="workspace-proxy-consent" type="checkbox" disabled><span id="workspace-proxy-consent-label">确认仅在此工作区使用上述匿名本机代理</span></label>
    <p>认证代理继续使用应用专用配置；检测到冲突时停止。不会将凭据转交给这里的新地址。保存不会访问代理或招聘网站。</p>
    <button id="save-workspace-proxy" type="button">保存工作区代理</button>
    <p id="workspace-proxy-status" role="status" aria-live="polite">正在读取工作区代理。</p>`;
  controls.append(proxy);
  const pac = document.createElement('fieldset');
  pac.style.cssText = 'min-width:0;max-width:100%';
  pac.innerHTML = `<legend>导入可信的 Windows PAC</legend>
    <p id="workspace-pac-disclosure"></p>
    <label for="workspace-pac-file">本地 PAC 文件</label>
    <input id="workspace-pac-file" type="file" accept=".pac,.js" style="max-width:100%;box-sizing:border-box">
    <label><input id="workspace-pac-consent" type="checkbox">我信任此文件，并同意上述脚本执行和DNS说明</label>
    <button id="save-workspace-pac" type="button">导入并启用 PAC</button>
    <button id="check-workspace-pac" type="button" disabled>检查脚本</button>
    <p>检查仅对内置域名执行脚本，不连接代理或招聘网站。撤销时在上方选择“自动发现”并保存；已有连接不会被强行中断。</p>
    <p id="workspace-pac-status" role="status" aria-live="polite" style="overflow-wrap:anywhere">尚未读取 PAC 状态。</p>`;
  controls.append(pac);
  const systemPac = document.createElement('fieldset');
  systemPac.style.cssText = 'min-width:0;max-width:100%;overflow-wrap:anywhere';
  systemPac.innerHTML = `<legend>使用 Windows 已配置的 PAC</legend>
    <p id="system-pac-disclosure"></p><p id="system-pac-origin"></p>
    <button id="refresh-system-pac" type="button">重新读取系统配置</button>
    <label><input id="system-pac-consent" type="checkbox">我信任上述来源现在和后续提供的脚本，并同意执行与DNS说明</label>
    <button id="save-system-pac" type="button" disabled>确认并使用系统 PAC</button>
    <button id="check-system-pac" type="button" disabled>读取并检查系统脚本</button>
    <p>检查会下载当前来源脚本并对内置域名求值，不连接岗位网站。新会话会重新读取该来源；来源地址变化须再次确认。撤销时在上方选择“自动发现”并保存。</p>
    <p id="system-pac-status" role="status" aria-live="polite"></p>`;
  controls.append(systemPac);
  const systemConsent = systemPac.querySelector('#system-pac-consent');
  const systemButton = systemPac.querySelector('#save-system-pac');
  const systemCheck = systemPac.querySelector('#check-system-pac');
  const systemMessage = systemPac.querySelector('#system-pac-status');
  let systemConfigId = '';
  const pacFile = pac.querySelector('#workspace-pac-file');
  const pacConsent = pac.querySelector('#workspace-pac-consent');
  const pacButton = pac.querySelector('#save-workspace-pac');
  const pacCheck = pac.querySelector('#check-workspace-pac');
  const pacMessage = pac.querySelector('#workspace-pac-status');
  pacFile.addEventListener('change', () => { pacConsent.checked = false; });
  const mode = proxy.querySelector('#workspace-proxy-mode');
  const endpoint = proxy.querySelector('#workspace-proxy-endpoint');
  const consent = proxy.querySelector('#workspace-proxy-consent');
  const proxyButton = proxy.querySelector('#save-workspace-proxy');
  const proxyMessage = proxy.querySelector('#workspace-proxy-status');
  let revision = null, busy = false;
  function selection() {
    endpoint.disabled = consent.disabled = ['auto', 'pac', 'system_pac'].includes(mode.value);
    proxyButton.disabled = ['pac', 'system_pac'].includes(mode.value);
    consent.checked = false;
    const vm = mode.value.startsWith('vm_');
    endpoint.placeholder = vm ? '填写宿主机已授权监听的 RFC1918 IPv4 和端口' : '填写你的本机代理入口，不含账号密码';
    proxy.querySelector('#workspace-proxy-consent-label').textContent = vm
      ? '确认该地址是宿主机授权给此来宾的匿名代理入口，仅在此工作区使用'
      : '确认仅在此工作区使用上述匿名本机代理';
    proxy.querySelector('#workspace-proxy-scope').textContent = vm
      ? '来宾的127.0.0.1不是宿主机。请填写已知的HTTP或SOCKS5完整地址，不自动猜网关或开启监听；代理可看到目标IP和流量时间。具体虚拟机环境尚需实测。'
      : '本机模式只接受此系统的loopback地址。';
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
      : (mode.value === 'auto' ? '采用自动发现；尚未进行网络测试。'
        : mode.value === 'pac' ? '按已导入PAC选择域名路线；尚未进行网络测试。'
        : mode.value === 'system_pac' ? '按已许可系统PAC选择域名路线；尚未进行网络测试。'
        : '已保存 '+endpoint.value+'，仅对本工作区生效；尚未进行网络测试。');
    pac.querySelector('#workspace-pac-disclosure').textContent = value.pac_disclosure || '';
    pacFile.disabled = pacConsent.disabled = pacButton.disabled = value.pac_available !== true;
    pacCheck.disabled = value.pac_available !== true || mode.value !== 'pac';
    pacConsent.checked = false;
    pacMessage.textContent = mode.value === 'pac'
      ? '已导入 '+value.pac_name+'（SHA256 '+value.pac_sha256+'）。尚未执行检查。'
        + (value.policy?.code === 'pac_file_invalid' ? ' 副本缺失或校验失败，联网已停止；可选择自动模式撤销。' : '')
      : '未启用PAC；选择文件不会执行脚本。';
    if (!value.pac_available) pacMessage.textContent += ' 当前系统不支持此Windows功能。';
    systemPac.querySelector('#system-pac-disclosure').textContent = value.system_pac_disclosure || '';
    const source = value.system_pac || {};
    systemConfigId = source.config_id || '';
    systemPac.querySelector('#system-pac-origin').textContent = source.configured
      ? '当前系统来源站点：'+source.origin+'（路径和参数隐藏）' : (source.message || '无法读取系统PAC配置');
    systemConsent.checked = false;
    systemConsent.disabled = systemButton.disabled = !value.pac_available || source.configured !== true;
    systemCheck.disabled = !value.pac_available || mode.value !== 'system_pac'
      || source.config_id !== value.system_pac_config_id;
    systemMessage.textContent = mode.value === 'system_pac'
      ? (source.config_id === value.system_pac_config_id ? '已许可此来源；本次状态读取没有下载或执行。'
        : '来源已改变或不可用，联网停止。请核对当前来源后重新确认，或选择自动发现撤销。')
      : '未启用系统PAC；读取配置不会下载脚本。';
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
  pacButton.addEventListener('click', async () => {
    if (busy || revision === null) return;
    const file = pacFile.files[0];
    if (!file || !pacConsent.checked) { pacMessage.textContent = '请选择可信文件并勾选执行说明。'; return; }
    if (file.size > 65536 || !/\.(pac|js)$/i.test(file.name)) { pacMessage.textContent = '请选择最多64KiB的.pac或.js文件。'; return; }
    const selectedRevision = revision;
    try {
      const script = new TextDecoder('utf-8', {fatal:true}).decode(await file.arrayBuffer());
      if (pacFile.files[0] !== file || !pacConsent.checked || selectedRevision !== revision || busy) return;
      await save('/api/network/pac', {name:file.name, script, consent:true}, pacMessage);
    } catch (_) { pacMessage.textContent = '无法读取UTF-8文件；原设置保留。'; }
  });
  pacCheck.addEventListener('click', async () => {
    if (busy || revision === null) return;
    busy = true; controls.disabled = true;
    pacMessage.textContent = '正在执行内置域名的脚本检查…';
    try { const value = await call('/api/network/pac/check', {revision}); pacMessage.textContent = value.message; }
    catch (error) { pacMessage.textContent = error.message; }
    finally { busy = false; controls.disabled = false; }
  });
  systemPac.querySelector('#refresh-system-pac').addEventListener('click', async () => {
    if (busy) return;
    busy = true; controls.disabled = true; systemConsent.checked = false;
    try { render(await call('/api/network/state')); }
    catch (error) { systemConfigId = ''; systemButton.disabled = systemCheck.disabled = true; systemMessage.textContent = error.message; }
    finally { busy = false; controls.disabled = false; }
  });
  systemButton.addEventListener('click', () => {
    if (!systemConsent.checked || !systemConfigId) { systemMessage.textContent = '请核对来源并勾选信任说明。'; return; }
    save('/api/network/system-pac', {config_id:systemConfigId, consent:true}, systemMessage);
    systemConsent.checked = false;
  });
  systemCheck.addEventListener('click', async () => {
    if (busy || revision === null) return;
    busy = true; controls.disabled = true;
    systemMessage.textContent = '正在读取已许可来源，并执行内置域名检查…';
    try { const value = await call('/api/network/pac/check', {revision}); systemMessage.textContent = value.message; }
    catch (error) { systemMessage.textContent = error.message; }
    finally { busy = false; controls.disabled = false; }
  });
  call('/api/network/state').then(value => {
    render(value);
    message.textContent = toggle.checked ? '已同意仅在映射地址时采用加密解析。尚未进行网络测试。' : '未开启额外解析；普通公网DNS不受影响。';
    button.disabled = false;controls.disabled = false;
  }).catch(error => { message.textContent = error.message;proxyMessage.textContent = '设置未就绪；请核对原配置后重新载入。'; });
})();
