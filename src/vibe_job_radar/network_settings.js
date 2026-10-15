/* Shared same-origin workspace preferences; discovery/checks require explicit actions. */
(() => {
  'use strict';
  const panel = document.getElementById('network-preferences');
  if (!panel) return;
  const toggle = panel.querySelector('#encrypted-dns-consent');
  const dnsScope = panel.querySelector('#network-dns-scope');
  const dnsDisclosure = panel.querySelector('#network-dns-disclosure');
  let dnsDisclosures = {};
  dnsScope.addEventListener('change', () => {
    toggle.checked = false;
    dnsDisclosure.textContent = dnsDisclosures[dnsScope.value] || '';
    message.textContent = '解析范围已选择但尚未保存；启用此范围请重新勾选同意。';
  });
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
    <p>自动模式沿用应用或系统配置。固定入口支持本机代理（Windows可加密保存凭据），或已授权给此来宾的匿名宿主机代理；更换后请停止并重开已有采集会话。</p>
    <label for="workspace-proxy-mode">连接方式</label>
    <select id="workspace-proxy-mode"><option value="auto">自动发现</option><option value="http">固定本机 HTTP</option><option value="socks5">固定本机 SOCKS5</option><option value="http_auth" disabled>本机 HTTP Basic＋保存凭据</option><option value="http_digest_auth" disabled>本机 HTTP Digest＋保存凭据</option><option value="socks5_auth" disabled>本机 SOCKS5＋保存凭据</option><option value="vm_http">宿主机 HTTP</option><option value="vm_socks5">宿主机 SOCKS5</option><option value="pac" disabled>已导入的 Windows 域名 PAC</option><option value="pac_domain" disabled>已导入的跨平台域名 PAC</option><option value="pac_domain_dns" disabled>跨平台域名 PAC 与 DNS 助手</option><option value="pac_url" disabled>已导入的完整网址 PAC</option><option value="pac_url_dns" disabled>完整网址 PAC 与 DNS 助手</option><option value="system_pac" disabled>已许可的系统 PAC</option><option value="wpad" disabled>已确认的 WPAD</option></select>
    <label for="workspace-proxy-endpoint">已配置的代理地址和端口</label>
    <input id="workspace-proxy-endpoint" type="text" autocomplete="off" spellcheck="false" style="max-width:100%;box-sizing:border-box" placeholder="填写你的本机代理入口，不含账号密码" disabled>
    <p id="workspace-proxy-scope"></p>
    <label><input id="workspace-proxy-consent" type="checkbox" disabled><span id="workspace-proxy-consent-label">确认仅在此工作区使用上述匿名本机代理</span></label>
    <fieldset id="workspace-proxy-auth-fields" hidden style="min-width:0;max-width:100%">
      <legend>代理凭据</legend><p id="workspace-proxy-auth-disclosure"></p>
      <label for="workspace-proxy-username">代理用户名</label>
      <input id="workspace-proxy-username" type="text" autocomplete="off" spellcheck="false" maxlength="255" style="max-width:100%;box-sizing:border-box">
      <label for="workspace-proxy-password">代理密码</label>
      <input id="workspace-proxy-password" type="password" autocomplete="new-password" maxlength="255" style="max-width:100%;box-sizing:border-box">
      <p>用户名和密码各1–255个可打印ASCII字符；用户名不含冒号。每次保存均须重新输入，完成或失败后清空输入框。</p>
    </fieldset>
    <p>已有应用专用配置继续可用；检测到冲突时停止。保存不会访问代理或招聘网站。</p>
    <button id="save-workspace-proxy" type="button">保存工作区代理</button>
    <button id="clear-workspace-proxy-auth" type="button" hidden>清除已保存凭据并停止新连接</button>
    <p id="workspace-proxy-status" role="status" aria-live="polite">正在读取工作区代理。</p>`;
  controls.append(proxy);
  const pac = document.createElement('fieldset');
  pac.style.cssText = 'min-width:0;max-width:100%';
  pac.innerHTML = `<legend>导入可信的 PAC 文件</legend>
    <label for="workspace-pac-kind">脚本执行范围</label>
    <select id="workspace-pac-kind"><option value="domain">Windows 域名 PAC</option><option value="portable_domain">跨平台域名 PAC</option><option value="portable_domain_dns">跨平台域名 PAC 与 DNS 助手（独立许可）</option><option value="url">完整网址 PAC（HTTP／请求桥）</option><option value="url_dns">完整网址 PAC 与 DNS 助手（独立许可）</option></select>
    <p id="workspace-pac-disclosure"></p>
    <label for="workspace-pac-file">本地 PAC 文件</label>
    <input id="workspace-pac-file" type="file" accept=".pac,.js" style="max-width:100%;box-sizing:border-box">
    <label><input id="workspace-pac-consent" type="checkbox"><span id="workspace-pac-consent-label">我信任此文件，并同意上述脚本执行和DNS说明</span></label>
    <button id="save-workspace-pac" type="button">导入并启用 PAC</button>
    <button id="check-workspace-pac" type="button" disabled>检查脚本</button>
    <p>检查使用内置的虚构网址，不连接代理或招聘网站；DNS助手模式会执行脚本要求的系统DNS查询。完整网址模式不支持原生浏览器通道。撤销时在上方选择“自动发现”并保存；已有连接不会被强行中断。</p>
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
  const wpad = document.createElement('fieldset');
  wpad.style.cssText = 'min-width:0;max-width:100%;overflow-wrap:anywhere';
  wpad.innerHTML = `<legend>发现并确认 Windows WPAD</legend>
    <p id="wpad-disclosure"></p>
    <label><input id="wpad-discovery-consent" type="checkbox">同意向当前网络发现PAC地址；此步不会下载或执行脚本</label>
    <button id="discover-wpad" type="button">发现来源</button>
    <button id="cancel-wpad" type="button" disabled>取消发现／清除预览</button>
    <p id="wpad-origin"></p>
    <label><input id="wpad-trust-consent" type="checkbox" disabled>我已核对来源站点，信任此完整地址现在和后续提供的脚本，并同意上述执行与DNS说明</label>
    <button id="save-wpad" type="button" disabled>确认并使用此来源</button>
    <button id="check-wpad" type="button" disabled>重新发现并检查脚本</button>
    <p>检查会重新发现、核对已确认地址并下载脚本，只对内置域名求值，不连接招聘网站。换网后请停止并重开会话。撤销许可时在上方选择“自动发现”并保存。</p>
    <p id="wpad-status" role="status" aria-live="polite"></p>`;
  controls.append(wpad);
  const wpadDiscoveryConsent = wpad.querySelector('#wpad-discovery-consent');
  const wpadTrustConsent = wpad.querySelector('#wpad-trust-consent');
  const wpadDiscover = wpad.querySelector('#discover-wpad');
  const wpadCancel = wpad.querySelector('#cancel-wpad');
  const wpadSave = wpad.querySelector('#save-wpad');
  const wpadCheck = wpad.querySelector('#check-wpad');
  const wpadMessage = wpad.querySelector('#wpad-status');
  let wpadCandidate = '', wpadPolling = null;
  function renderWpad(value) {
    const source = value.wpad || {};
    wpad.querySelector('#wpad-disclosure').textContent = value.wpad_disclosure || '';
    const nextCandidate = source.candidate || '';
    if (nextCandidate !== wpadCandidate) wpadTrustConsent.checked = false;
    wpadCandidate = nextCandidate;
    wpad.querySelector('#wpad-origin').textContent = wpadCandidate
      ? '发现的来源站点：'+source.origin+'（完整地址摘要 '+source.config_id+'；路径和参数隐藏）'
      : '当前没有待确认的来源预览。';
    const available = source.available === true && value.pac_available === true;
    wpadDiscover.disabled = wpadDiscoveryConsent.disabled = !available || source.discovery_running === true;
    wpadTrustConsent.disabled = wpadSave.disabled = !available || !wpadCandidate || source.discovery_running === true;
    wpadCancel.disabled = !source.discovery_running && !wpadCandidate;
    wpadCheck.disabled = !available || value.proxy_mode !== 'wpad' || source.discovery_running === true;
    wpadMessage.textContent = (value.proxy_mode === 'wpad'
      ? '已确认来源；本次状态读取未探测、下载或执行。新会话会重新核对。 ' : '') + (source.message || '');
    if (!available) wpadMessage.textContent += ' 当前系统不支持此Windows功能。';
    clearTimeout(wpadPolling);
    if (source.discovery_running) wpadPolling = setTimeout(pollWpad, 300);
  }
  async function pollWpad() {
    try {
      const value = await call('/api/network/state');
      if (value.revision !== revision) render(value);
      else renderWpad(value);
    } catch (error) {
      wpadCandidate = ''; wpadTrustConsent.checked = false;
      wpadTrustConsent.disabled = wpadSave.disabled = true;
      wpadMessage.textContent = error.message+'；请重新载入页面核对状态。';
    }
  }
  const systemConsent = systemPac.querySelector('#system-pac-consent');
  const systemButton = systemPac.querySelector('#save-system-pac');
  const systemCheck = systemPac.querySelector('#check-system-pac');
  const systemMessage = systemPac.querySelector('#system-pac-status');
  let systemConfigId = '';
  const pacKind = pac.querySelector('#workspace-pac-kind');
  const pacScopes = {
    domain: {mode:'pac', available:'pac_available', disclosure:'pac_disclosure', endpoint:'/api/network/pac',
      consent:'我信任此文件，并同意上述Windows脚本执行和DNS说明'},
    portable_domain: {mode:'pac_domain', available:'pac_domain_available', disclosure:'pac_domain_disclosure', endpoint:'/api/network/pac-domain',
      consent:'我信任此文件，同意只向脚本提供HTTPS根地址和域名；不提供DNS助手'},
    portable_domain_dns: {mode:'pac_domain_dns', available:'pac_domain_available', disclosure:'pac_domain_dns_disclosure', endpoint:'/api/network/pac-domain-dns',
      consent:'我信任此文件，同意域名选路及上述系统DNS查询和本机IPv4地址范围'},
    url: {mode:'pac_url', available:'pac_url_available', disclosure:'pac_url_disclosure', endpoint:'/api/network/pac-url',
      consent:'我信任此文件，同意向脚本提供完整HTTPS路径和查询参数，并知悉此模式不支持原生浏览器'},
    url_dns: {mode:'pac_url_dns', available:'pac_url_available', disclosure:'pac_url_dns_disclosure', endpoint:'/api/network/pac-url-dns',
      consent:'我信任此文件，同意完整网址、系统DNS查询和本机IPv4地址的上述范围'}
  };
  let pacState = {};
  const pacFile = pac.querySelector('#workspace-pac-file');
  const pacConsent = pac.querySelector('#workspace-pac-consent');
  const pacButton = pac.querySelector('#save-workspace-pac');
  const pacCheck = pac.querySelector('#check-workspace-pac');
  const pacMessage = pac.querySelector('#workspace-pac-status');
  pacFile.addEventListener('change', () => { pacConsent.checked = false; });
  function renderPacScope() {
    const scope = pacScopes[pacKind.value];
    const savedMode = scope.mode, available = pacState[scope.available] === true;
    pac.querySelector('#workspace-pac-disclosure').textContent = pacState[scope.disclosure] || '';
    pac.querySelector('#workspace-pac-consent-label').textContent = scope.consent;
    pacFile.disabled = pacConsent.disabled = pacButton.disabled = !available;
    pacCheck.disabled = !available || pacState.proxy_mode !== savedMode;
    pacConsent.checked = false;
    pacMessage.textContent = pacState.proxy_mode === savedMode
      ? '已导入 '+pacState.pac_name+'（SHA256 '+pacState.pac_sha256+'）。尚未执行检查。'
        + (pacState.policy?.code === 'pac_file_invalid' ? ' 副本缺失或校验失败，联网已停止；可选择自动模式撤销。' : '')
      : '当前范围尚未启用；选择文件或范围不会执行脚本。';
    if (!available) pacMessage.textContent += pacKind.value === 'domain'
      ? ' 当前系统不支持此Windows功能。'
      : ' 当前程序缺少跨平台PAC组件，请使用包含此组件的版本。';
  }
  pacKind.addEventListener('change', renderPacScope);
  const mode = proxy.querySelector('#workspace-proxy-mode');
  const endpoint = proxy.querySelector('#workspace-proxy-endpoint');
  const consent = proxy.querySelector('#workspace-proxy-consent');
  const proxyButton = proxy.querySelector('#save-workspace-proxy');
  const proxyMessage = proxy.querySelector('#workspace-proxy-status');
  const authFields = proxy.querySelector('#workspace-proxy-auth-fields');
  const proxyUsername = proxy.querySelector('#workspace-proxy-username');
  const proxyPassword = proxy.querySelector('#workspace-proxy-password');
  const authClear = proxy.querySelector('#clear-workspace-proxy-auth');
  let authAvailable = false, authSaved = false, savedMode = '', savedEndpoint = '';
  const isAuth = () => ['http_auth', 'http_digest_auth', 'socks5_auth'].includes(mode.value);
  function clearSecretInputs() { proxyUsername.value = ''; proxyPassword.value = ''; }
  let revision = null, busy = false;
  function selection() {
    endpoint.disabled = consent.disabled = ['auto', 'pac', 'pac_domain', 'pac_domain_dns', 'pac_url', 'pac_url_dns', 'system_pac', 'wpad'].includes(mode.value);
    proxyButton.disabled = ['pac', 'pac_domain', 'pac_domain_dns', 'pac_url', 'pac_url_dns', 'system_pac', 'wpad'].includes(mode.value);
    consent.checked = false;
    const vm = mode.value.startsWith('vm_'), auth = isAuth();
    clearSecretInputs();
    authFields.hidden = !auth;
    proxyUsername.disabled = proxyPassword.disabled = !auth || !authAvailable;
    authClear.hidden = !auth;
    authClear.disabled = !authSaved || mode.value !== savedMode || endpoint.value !== savedEndpoint;
    if (auth && !authAvailable) proxyButton.disabled = true;
    proxyButton.textContent = auth ? '加密保存工作区代理凭据' : '保存工作区代理';
    endpoint.placeholder = vm ? '填写宿主机已授权监听的 RFC1918 IPv4 和端口' : '填写你的本机代理入口，不含账号密码';
    proxy.querySelector('#workspace-proxy-consent-label').textContent = vm
      ? '确认该地址是宿主机授权给此来宾的匿名代理入口，仅在此工作区使用'
      : auth ? '同意按上述说明将代理凭据加密保存在当前Windows账号和此工作区，仅交付上述本机入口'
      : '确认仅在此工作区使用上述匿名本机代理';
    proxy.querySelector('#workspace-proxy-scope').textContent = vm
      ? '来宾的127.0.0.1不是宿主机。请填写已知的HTTP或SOCKS5完整地址，不自动猜网关或开启监听；代理可看到目标IP和流量时间。具体虚拟机环境尚需实测。'
      : '本机模式只接受此系统的loopback地址。';
  }
  mode.addEventListener('change', selection);
  endpoint.addEventListener('input', () => { consent.checked = false; clearSecretInputs(); authClear.disabled = true; });
  for (const field of [proxyUsername, proxyPassword]) field.addEventListener('input', () => { consent.checked = false; });
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
    toggle.checked = value.mode !== 'system';
    dnsScope.value = value.mode === 'public_doh' ? 'public_doh' : 'fake_ip_doh';
    dnsDisclosures = value.dns_disclosures || {fake_ip_doh:value.disclosure || ''};
    dnsDisclosure.textContent = dnsDisclosures[dnsScope.value] || '';
    revision = value.revision;
    authAvailable = value.proxy_auth_available === true;
    authSaved = value.proxy_auth_saved === true;
    savedMode = value.proxy_mode || 'auto'; savedEndpoint = value.proxy_endpoint || '';
    for (const name of ['http_auth','http_digest_auth','socks5_auth']) mode.querySelector('option[value='+name+']').disabled = !authAvailable;
    proxy.querySelector('#workspace-proxy-auth-disclosure').textContent = value.proxy_auth_disclosure || '';
    mode.value = value.proxy_mode || 'auto';
    endpoint.value = value.proxy_endpoint || '';
    selection();
    proxyMessage.textContent = value.policy?.code === 'workspace_proxy_environment_conflict'
      ? '工作区代理与应用专用环境配置冲突，联网已停止。请清除冲突或改用自动模式。'
      : (mode.value === 'auto' ? '采用自动发现；尚未进行网络测试。'
        : mode.value === 'pac' ? '按已导入PAC选择域名路线；尚未进行网络测试。'
        : ['pac_domain', 'pac_domain_dns'].includes(mode.value) ? '按已导入PAC选择域名路线，适用于HTTP、请求桥和原生浏览器；尚未进行网络测试。'
        : ['pac_url', 'pac_url_dns'].includes(mode.value) ? '按已导入PAC选择完整网址路线，仅支持HTTP和请求桥；尚未进行网络测试。'
        : mode.value === 'system_pac' ? '按已许可系统PAC选择域名路线；尚未进行网络测试。'
        : mode.value === 'wpad' ? '按已确认WPAD来源选择域名路线；尚未进行网络测试。'
        : '已保存 '+endpoint.value+'，仅对本工作区生效；尚未进行网络测试。');
    if (isAuth()) proxyMessage.textContent = value.proxy_auth_message ||
      (value.policy?.code === 'workspace_proxy_environment_conflict' ? proxyMessage.textContent :
        (authSaved ? '代理凭据已加密保存；页面不会回显，尚未联网测试。' : '未保存代理凭据，新连接已停止。'));
    pacState = value;
    pacKind.value = Object.keys(pacScopes).find(key => pacScopes[key].mode === mode.value) || 'domain';
    renderPacScope();
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
    wpadDiscoveryConsent.checked = wpadTrustConsent.checked = false;
    renderWpad(value);
  }
  async function save(path, data, output) {
    if (revision === null || busy) return;
    busy = true;controls.disabled = true;
    try {
      const value = await call(path, {...data, revision});
      render(value);if (typeof value.message === 'string') output.textContent = value.message;
    } catch (error) { output.textContent = error.message; }
    finally { busy = false;controls.disabled = false; }
  }
  button.addEventListener('click', () => save('/api/network/preferences',
    {mode: toggle.checked ? dnsScope.value : 'system', consent: toggle.checked}, message));
  proxyButton.addEventListener('click', async () => {
    if (busy || revision === null) return;
    const auth = isAuth();
    try {
      await save(auth ? '/api/network/proxy-auth' : '/api/network/proxy',
        {mode: mode.value, endpoint: mode.value === 'auto' ? '' : endpoint.value,
          consent: mode.value === 'auto' ? false : consent.checked,
          ...(auth ? {username:proxyUsername.value, password:proxyPassword.value} : {})}, proxyMessage);
    } finally { clearSecretInputs(); consent.checked = false; }
  });
  authClear.addEventListener('click', () => {
    clearSecretInputs();
    save('/api/network/proxy-auth/clear', {consent:true}, proxyMessage);
  });
  pacButton.addEventListener('click', async () => {
    if (busy || revision === null) return;
    const file = pacFile.files[0];
    if (!file || !pacConsent.checked) { pacMessage.textContent = '请选择可信文件并勾选执行说明。'; return; }
    if (file.size > 65536 || !/\.(pac|js)$/i.test(file.name)) { pacMessage.textContent = '请选择最多64KiB的.pac或.js文件。'; return; }
    const selectedRevision = revision, selectedKind = pacKind.value;
    try {
      const script = new TextDecoder('utf-8', {fatal:true}).decode(await file.arrayBuffer());
      if (pacFile.files[0] !== file || !pacConsent.checked || selectedRevision !== revision ||
          selectedKind !== pacKind.value || busy) return;
      await save(pacScopes[selectedKind].endpoint,
        {name:file.name, script, consent:true}, pacMessage);
    } catch (_) { pacMessage.textContent = '无法读取UTF-8文件；原设置保留。'; }
  });
  pacCheck.addEventListener('click', async () => {
    if (busy || revision === null) return;
    busy = true; controls.disabled = true;
    pacMessage.textContent = '正在执行内置虚构网址的脚本检查…';
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
  wpadDiscover.addEventListener('click', () => {
    if (!wpadDiscoveryConsent.checked) {
      wpadMessage.textContent = '请先勾选同意向当前网络发现地址。'; return;
    }
    wpadCandidate = ''; wpadTrustConsent.checked = false;
    save('/api/network/wpad/discover', {consent:true}, wpadMessage);
    wpadDiscoveryConsent.checked = false;
  });
  wpadCancel.addEventListener('click', async () => {
    if (busy) return;
    busy = true; controls.disabled = true;
    try { render(await call('/api/network/wpad/cancel', {})); }
    catch (error) { wpadMessage.textContent = error.message; }
    finally { busy = false; controls.disabled = false; }
  });
  wpadSave.addEventListener('click', () => {
    if (!wpadTrustConsent.checked || !wpadCandidate) {
      wpadMessage.textContent = '请核对来源并单独勾选信任说明。'; return;
    }
    save('/api/network/wpad', {candidate:wpadCandidate, consent:true}, wpadMessage);
    wpadTrustConsent.checked = false;
  });
  wpadCheck.addEventListener('click', async () => {
    if (busy || revision === null) return;
    busy = true; controls.disabled = true;
    wpadMessage.textContent = '正在重新发现并核对来源，再读取和检查脚本…';
    try { const value = await call('/api/network/pac/check', {revision}); wpadMessage.textContent = value.message; }
    catch (error) { wpadMessage.textContent = error.message; }
    finally { busy = false; controls.disabled = false; }
  });
  call('/api/network/state').then(value => {
    render(value);
    message.textContent = !toggle.checked ? '未开启额外解析；继续使用系统DNS。'
      : value.mode === 'public_doh' ? '已同意公共域名使用Cloudflare；尚未进行网络测试。'
      : '已同意仅在映射地址时采用加密解析。尚未进行网络测试。';
    button.disabled = false;controls.disabled = false;
  }).catch(error => { message.textContent = error.message;proxyMessage.textContent = '设置未就绪；请核对原配置后重新载入。'; });
})();
