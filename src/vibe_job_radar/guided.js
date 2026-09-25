"use strict";
const $ = id => document.getElementById(id);
const intake = new URLSearchParams(location.hash.slice(1));
let intakeApplied = false;
const token = intake.get('token') || sessionStorage.getItem('radar-session') || '';
if (token) sessionStorage.setItem('radar-session', token);
if (location.hash) history.replaceState(null,'',location.pathname+location.search);
let requestedTask=new URLSearchParams(location.search).get('task')||'';
let state=null, current=null, selecting=new Set(), loadedId='', requesting=false, sticky='';
const installationNames={restart_required:'组件更新后需重启工作台再检查',not_started:'本次会话未执行安装（不表示缺少组件）',installing:'正在安装',installed:'安装并启动验证成功',installed_not_ready:'安装命令成功，但启动检查失败',dependency_install_failed:'安装失败',dependency_install_timeout:'安装超时'};
const cardStatus={job_unavailable:'岗位已暂停招聘或下线',job_identity_mismatch:'详情身份不一致，未保存',jd_incomplete:'正文尚未完整展开，未保存',discovered:'待选择',opening:'读取中',ok:'正文已保存',structure_changed:'无法确认独立完整正文',invalid_job_data:'岗位字段无效或正文超出限制',not_job_url:'不是已识别的详情地址',manual_required:'需要正常登录或验证',http_401:'需要核对登录或权限',http_403:'站点拒绝访问',http_429:'来源要求等待',paused:'已暂停',network_error:'网络未完成'};
const statusNames={queued:'准备中',running:'执行中',ready:'可以选择岗位',waiting_rate:'按来源要求等待',waiting_manual:'需要你处理',paused:'已暂停',completed:'批次结束',stopped:'已停止',interrupted:'上次服务已退出'};
Object.assign(cardStatus,{read_transient_failure:'来源暂时不可用，等待有限重试',read_retry_exhausted:'两次自动重试已用完',read_retry_after_invalid:'来源重试时间需核对',read_retry_unavailable:'无法确认自动重试条件'});
async function api(path,data){const response=await fetch(path,{method:data===undefined?'GET':'POST',headers:{'X-Radar-Token':token,...(data===undefined?{}:{'Content-Type':'application/json'})},body:data===undefined?undefined:JSON.stringify(data),cache:'no-store'});const result=await response.json();if(!response.ok)throw Error(result.error||'操作失败');return result;}
function note(text){$('busy').textContent=text;}
async function act(fn){if(requesting)return;requesting=true;sticky='';try{await fn();await refresh();}catch(e){sticky=e.message;note(sticky);}finally{requesting=false;}}
function options(element,values){const value=element.value;element.replaceChildren();values.forEach(([id,label])=>{const o=document.createElement('option');o.value=id;o.textContent=label;element.append(o);});if(values.some(x=>x[0]===value))element.value=value;}
function active(){if(!current)throw Error('先在第2步创建任务。');return current.id;}
function render(){
 if(!state)return;
 const foreign=state.owned_elsewhere===true;
 $('guided-startup').disabled=false;$('guided-startup').setAttribute('aria-busy','false');$('guided-initializing').hidden=true;
 const portable=state.runtime?.kind==='portable';
 $('environment').textContent=portable ? `便携运行包 · Playwright：${state.browser_package||'组件缺失'}。` : `当前 Python：${state.python}。Playwright：${state.browser_package||'尚未安装'}。本次组件操作：${installationNames[state.installation]||state.installation}。`;
 if(!$('portable-runtime-note')){const p=document.createElement('p');p.id='portable-runtime-note';p.className='notice';$('environment').after(p);}
 $('portable-runtime-note').hidden=!portable;$('portable-runtime-note').textContent=state.runtime?.guidance||'';
 for(const id of ['install','repair-browser','upgrade-browser','source-runtime-help','source-browser-instructions'])$(id).hidden=portable;
 const tls=state.tls_environment;
 if(tls){
  $('tls-repair').hidden=!tls.windows||portable;
  $('tls-environment').textContent='TLS 验证引擎：'+tls.engine+' · '+tls.reason+(tls.restart_required?' · 组件操作后需重新启动工作台再检查':'');
 }
 const choice=state.browser_choice;
 if(choice){
  if(!$('browser-choice').options.length){options($('browser-choice'),Object.entries(choice.options));$('browser-choice').value=choice.selected||'bundled';}
  const previous=choice.last_check;
  $('browser-history').textContent=choice.error || (previous
   ? `上次组件检查（历史，不代表本次就绪）：${previous.checked_at} · ${choice.options[previous.channel]||previous.channel} · Playwright ${previous.playwright_version} · ${previous.code}${previous.exit_hex?' · '+previous.exit_hex:''}。${previous.matches_environment?'同一解释器和SDK；仍需本次检查。':'环境或SDK已变化；不能沿用旧结果。'}`
   : '尚无已保存的组件检查记录；不表示未安装，也不会自动安装。');
  $('browser-selected').textContent='当前采集浏览器：'+(choice.options[choice.selected]||'尚无法读取选择')+'。更换须空白页检查通过；不接管日常浏览器或保存其登录态。';
 }
 const health=state.browser_health;
 if(health){$('browser-summary').textContent=(health.browser_channel ? '本次检查：'+(choice?.options[health.browser_channel]||health.browser_channel)+'。' : '')+health.message;
 $('browser-diagnostic').textContent=JSON.stringify({browser:health,installation:state.setup,choice},null,2);}
 if(!$('site').options.length)options($('site'),state.sites.map(s=>[s.key,s.label+'（实站未验证）']));
 $('capability-status').replaceChildren();
 for(const site of state.sites){
  for(const capability of site.acquisition?.backends || []){
   const line=document.createElement('p');
   line.textContent=`${site.label} · ${capability.backend==='bridge'?'默认浏览器桥':'原生实验'}：${capability.message}`;
   $('capability-status').append(line);
  }
 }
 if(!$('role').options.length){options($('role'),Object.entries(state.roles));$('role').value='time_series';}
 if(!intakeApplied){
  intakeApplied=true;
  if(intake.has('role')||intake.has('platform')||intake.has('keyword')){
    const role=intake.get('role'),platform=intake.get('platform'),keyword=intake.get('keyword')||'';
    if(Object.hasOwn(state.roles,role)&&state.sites.some(s=>s.key===platform)&&keyword.trim()&&keyword.length<=100&&!/[\x00-\x1f]/.test(keyword)){
      $('role').value=role;$('site').value=platform;
      $('search-form').elements.keyword.value=keyword;
      sticky='已带入研究目标，尚未访问平台。请核对预算与实际访问范围，再确认开始。';
    }else sticky='目标链接无效，未按该链接创建任务或访问平台。';
  }
}
 $('limits').textContent=`服务端硬限制：页面导航至少 ${state.limits.page_interval} 秒，最多 ${state.limits.pages_hour} 次/小时；桥接或原生已计量的 HTTP 请求至少 ${state.limits.request_interval} 秒，最多 ${state.limits.requests_hour} 次/小时。不同任务共享配额，不能从界面提高。`;
 options($('task'),state.jobs.map(j=>[j.id,`${j.platform} · ${j.keyword} · ${statusNames[j.status]||j.status}`]));
 if(requestedTask&&state.jobs.some(j=>j.id===requestedTask)){$('task').value=requestedTask;requestedTask='';}
 current=state.jobs.find(j=>j.id===$('task').value)||null;
 $('password-login').hidden=!current || current.platform!=='liepin';
 if(current&&loadedId!==current.id){selecting=new Set(current.selection||[]);loadedId=current.id;}
 $('login-return-status').textContent=current?({watching:'等待平台显示账号区域及可用搜索框，或返回原列表/所选完整详情；连续确认后继续原任务，最长10分钟。',checking_search:'已观察到账号区域和搜索框，正在重新核对并提交原关键词一次。',resumed_search:'已从登录入口继续原关键词搜索；是否已登录以网站实际显示为准。',checking_detail:'已观察到所选完整详情，正在重新核对身份和正文；不重复请求该岗位。',resumed_detail:'已从登录返回的所选详情接回原采集和报告；不等于账号认证证明。',resumed:'已识别本任务可读列表，已自动接回任务；不等于账号认证证明。',timed_out:'自动接续等待已结束；会话未删除，可按原按钮继续。',needs_attention:'当前页面或网络需要处理，自动接续已停止。',cancelled:'自动接续已取消。'}[current.login_continuation]||''):'';
 $('saved-session-status').textContent=current?({empty:'本机尚无可用的保存会话；公开可读页面可直接采集，平台要求时再正常登录。',restored_unverified:'已恢复本站 Cookie；仍须由正常页面确认是否有效，未自动填写密码。',saved_unverified:'本站 Cookie 已保存到本机，供下次启动尝试恢复；不等于登录已认证。',expired:'本机快照已过 7 天，未恢复；请按平台正常流程重新登录。',cleared:'此平台保存会话已清除，旧任务不会自动重新启用保存。',save_failed:'本批结果已保留，但会话保存失败：'+(current.saved_session_error||'未知错误')}[current.saved_session_status]||''):'';
 $('session-reuse-status').textContent=current?.session_reused?'已复用当前采集浏览器会话；是否仍然登录以平台正常响应为准。停止或退出会关闭浏览器；仅明确启用的本站 Cookie 快照可供下次恢复。':'';
 if(current){$('resume').textContent=current.authentication==='manual_pending'?'登录完成，继续原任务':'继续原任务';$('task-status').textContent=`${current.backend==='native'?'原生网络实验':'原有HTTP桥'} · ${statusNames[current.status]||current.status}：${current.message}`;
 if(current.code==='non_public_address'||current.code==='dns_error'||current.code?.startsWith('encrypted_dns_')){
 $('task-status').textContent+='\n此页面请求由本程序在网络校验阶段中止；采集浏览器可能显示 ERR_BLOCKED_BY_CLIENT。它不等于平台封禁或 Edge 自身拒绝。请检查同一平台的当前网络策略；旧任务错误与新诊断不是同一次请求。';
 }
 if(current.read_retry){$('task-status').textContent+=`\n本任务已安排 ${current.read_retry.used}/2 次自动读页重试，重新启动或继续不会补回额度。`;}
 if(current.status==='waiting_rate'&&current.next_allowed_at){const remaining=Math.max(0,Math.ceil(current.next_allowed_at-Date.now()/1000));$('task-status').textContent+=`\n下次允许时间：${new Date(current.next_allowed_at*1000).toLocaleString()}（约 ${remaining} 秒）。${current.automatic_resume_available?'保留会话，到时自动继续。':'会话已退出或此动作需确认，届时点击继续；不必重填条件。'}`;}
 $('audit').textContent=JSON.stringify(current,null,2);renderCards();
 $('result').replaceChildren(document.createTextNode(`本批已保存 ${current.cards.filter(c=>c.status==='ok').length} 个岗位；发现 ${current.cards.length} 个候选链接。`));
 if(current.outcome){
   const o=current.outcome,summary=document.createElement('p'),counts=document.createElement('p');
   summary.id='acquisition-outcome';summary.className='warning';summary.textContent=o.message;
   counts.id='acquisition-counts';counts.textContent=`发现 ${o.discovered??current.cards.length} · 所选 ${o.selected} · 完整正文 ${o.full_jd??o.saved} · 目标岗位 ${o.target_relevant??o.target_jobs} · 有明确AI要求的岗位 ${o.jobs_with_explicit_ai_requirements??o.ai_jobs} · 失败 ${o.failed} · 待处理 ${o.pending}`;
   if(o.requirement_rows!==undefined){const rows=document.createElement('p');rows.textContent=`提取要求 ${o.requirement_rows} 条 · 已接收正向要求 ${o.accepted_positive_requirement_rows} 条 · 待复核 ${o.review_pending_rows} 条。要求条数与岗位数分别计算。`;$('result').append(rows);}
   $('result').append(summary,counts);
 }
 if(current.report_id){for(const [file,label] of [['requirements_zh.csv','下载岗位要求 CSV'],['descriptions.md','下载描述模板'],...(current.outcome ? [['guided_acquisition.json','下载本批采集结果']] : [])]){const b=document.createElement('button');b.className='secondary';b.textContent=label;const id=current.report_id;b.onclick=()=>act(()=>downloadReport(id,file));$('result').append(b);}const view=document.createElement('a');view.href='/#report='+current.report_id;view.textContent=' 查看本批研究结论';const a=document.createElement('a');a.href='/advanced#report='+current.report_id;a.textContent=' 用本批要求进入个人证据中心';$('result').append(view);if(!current.outcome||current.outcome.target_jobs>0)$('result').append(a);}
 }
 for(const button of document.querySelectorAll('button'))button.disabled=foreign ? !['copy-browser-diagnostic','export'].includes(button.id) : state.busy && !['pause','stop'].includes(button.id);
 if(tls) $('repair-tls').disabled=foreign||state.busy||tls.restart_required||!tls.repair_available;
 for(const input of document.querySelectorAll('#password-login-form input'))input.disabled=foreign;
 note([state.ownership_message, state.closure_uncertain?'无法确认上次采集浏览器已关闭，请退出原工作台进程并核对浏览器后重开。':'', sticky || (state.busy?(!state.active?state.setup?.message:current?.message)||'正在运行后端操作；可以暂停或停止。':''), ...(state.checkpoint_warnings||[])].filter(Boolean).join('\n'));
}
function renderCards(){const root=$('cards');root.replaceChildren();if(!current.cards.length){root.textContent=current.code==='no_matching_jobs'?current.message:'尚未取得可用岗位清单。请查看上方任务状态；仅在平台明确要求时处理登录，不必先提供密码。';return;}
 current.cards.forEach(c=>{const box=document.createElement('div');box.className='card';const label=document.createElement('label');const input=document.createElement('input');input.type='checkbox';input.checked=selecting.has(c.id);input.addEventListener('change',()=>{if(input.checked)selecting.add(c.id);else selecting.delete(c.id);});label.append(input,document.createTextNode(' '+c.title));const source=document.createElement('small');source.textContent='列表观察到的链接：'+c.url;const outcome=document.createElement('small');outcome.textContent='结果：'+(cardStatus[c.status]||c.status)+(c.resolved_url?' · 详情真实地址：'+c.resolved_url:'');box.append(label,source,outcome);root.append(box);});}
async function refresh(){state=await api('/api/guided/state');render();}
$('search-form').addEventListener('submit',e=>{e.preventDefault();const f=e.currentTarget;act(async()=>{const data=Object.fromEntries(new FormData(f));data.roles=[data.role];delete data.role;data.max_pages=Number(data.max_pages);data.max_jobs=Number(data.max_jobs);data.consent=f.elements.consent.checked;data.diagnostics=f.elements.diagnostics.checked;data.reuse_current_session=f.elements.reuse_current_session.checked;data.persist_session=f.elements.persist_session.checked;data.auto_collect=f.elements.auto_collect.checked;data.native_consent=f.elements.native_consent.checked;const r=await api('/api/guided/create',data);loadedId='';await refresh();$('task').value=r.id;render();});});
$('task').addEventListener('change',()=>{loadedId='';render();});
$('password-login-form').addEventListener('submit', e=>{
 e.preventDefault();
 if(requesting || state?.busy)return;
 const form=e.currentTarget;
 const data={id:active(),action:'login_password',username:form.elements.username.value,
  password:form.elements.password.value,credential_consent:form.elements.credential_consent.checked};
 // Clear the local controls immediately, including on a failed request. Never
 // put these values in state, storage, URLs, notes, console output or downloads.
 form.reset();
 act(async()=>{try{await api('/api/guided/action',data);}finally{data.username='';data.password='';}});
});
for(const [button,action] of Object.entries({'login':'login','capture':'capture','search-again':'search','pause':'pause','resume':'resume','stop':'stop'}))$(button).addEventListener('click',()=>act(()=>api('/api/guided/action',{id:active(),action,...(action==='login'?{auto_continue:$('auto-login-return').checked}:{})})));
$('collect').addEventListener('click',()=>act(()=>api('/api/guided/action',{id:active(),action:'collect',selected:[...selecting]})));
$('select-all').addEventListener('click',()=>{if(!current)return;selecting=new Set(current.cards.slice(0,current.max_jobs).map(c=>c.id));renderCards();});
$('use-browser-choice').addEventListener('click',()=>{
 const channel=$('browser-choice').value;
 if(confirm('将用所选浏览器打开独立空白页；只有检查通过后才保存选择，后续采集沿用原网络和访问规则。不安装系统浏览器、不接管日常标签页或登录资料；已有采集会话需先停止。是否继续？'))
  act(()=>api('/api/guided/check_browser',{channel,consent:true}));
});
$('check-browser').addEventListener('click',()=>act(()=>api('/api/guided/check_browser',{})));
$('copy-browser-diagnostic').addEventListener('click',()=>act(async()=>{const text=$('browser-diagnostic').textContent;try{await navigator.clipboard.writeText(text);sticky='诊断已复制；分享前可遮住本机用户名。';}catch{const selection=getSelection();const range=document.createRange();range.selectNodeContents($('browser-diagnostic'));selection.removeAllRanges();selection.addRange(range);sticky='浏览器未允许自动复制；已选中诊断，请按 Ctrl+C。';}}));
$('install').addEventListener('click',()=>{if(confirm('将使用当前Python检查/安装Playwright，并通过Playwright下载配套Chromium，然后实际打开空白浏览器验证。不会访问招聘网站；现有登录会话需先停止。是否继续？'))act(()=>api('/api/guided/install',{consent:true}));});
// Separate explicit repairs: no repeated no-op install and no silent SDK upgrade.
$('repair-browser').addEventListener('click',()=>{
 if(confirm('将重新下载当前 Playwright 配套的 Chromium，而不是直接复用已有缓存。不修改招聘数据、系统 DNS 或安全策略。请先停止已有采集会话；是否继续？'))
  act(()=>api('/api/guided/install',{consent:true,mode:'reinstall'}));
});
$('upgrade-browser').addEventListener('click',()=>{
 if(confirm('将更新当前 Python 中的 Playwright 等可选组件，并重新下载匹配 Chromium；这会影响共用该 Python 的其他项目。完成后需退出并重新启动工作台，再检查浏览器。不删除岗位或证据，不关闭系统防护。是否明确同意更新？'))
  act(()=>api('/api/guided/install',{consent:true,mode:'upgrade'}));
});
$('repair-tls').addEventListener('click',()=>{
 if(confirm('仅安装 truststore 原生证书验证组件，不更新 Playwright 或浏览器。Windows 可按系统策略获取中间证书；这些证书服务请求使用系统路由，不受岗位采集代理/配额控制。不导入网站证书，不关闭校验。现有采集会话须先停止，完成后需重新启动工作台。是否继续？'))
  act(()=>api('/api/guided/install',{consent:true,mode:'tls'}));
});
$('network').addEventListener('click',()=>act(async()=>{const r=await api('/api/guided/diagnose',{platform:$('site').value});if(r.tls_environment?.repair_available&&r.effective_resolution?.tls_diagnostic?.verification_reason==='issuer_unavailable')$('tls-repair').open=true;$('diagnostic').hidden=false;$('diagnostic').textContent=(r.message||'请分别核对系统解析和应用解析结果。')+'\n\n'+JSON.stringify(r,null,2);}));
$('export').addEventListener('click',()=>act(async()=>{const r=await api('/api/guided/export',{id:active()});const url=URL.createObjectURL(new Blob([r.urls.join('\n')],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='observed-job-urls.txt';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}));
if(!token)note('请从启动终端的完整令牌地址打开基础工作台，再进入本向导。');else refresh().catch(e=>note(e.message));
setInterval(()=>{if(token&&!requesting)refresh().catch(()=>{});},2000);

async function downloadReport(id,file){const r=await fetch(`/api/download/${id}/${file}`,{headers:{'X-Radar-Token':token},cache:'no-store'});if(!r.ok)throw Error('下载失败，请查看报告是否完整。');const u=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=u;a.download=file;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);}

// Explicit snapshot and download only; never export the full task/audit object.
let tracePreview=null;
$('enable-acquisition-trace').addEventListener('click',()=>act(async()=>{
 const r=await api('/api/guided/diagnostics',{id:active(),enabled:true});
 tracePreview=null;$('acquisition-trace').textContent=r.enabled?'诊断已启用。继续正常任务后再预览；没有自动发起采集。':'诊断未启用。';
}));
$('preview-acquisition-trace').addEventListener('click',()=>act(async()=>{
 const id=active();const r=await api('/api/guided/diagnostics',{id});
 tracePreview={id,data:r};$('acquisition-trace').textContent=JSON.stringify(r,null,2);
}));
$('disable-acquisition-trace').addEventListener('click',()=>act(async()=>{
 await api('/api/guided/diagnostics',{id:active(),enabled:false});
 tracePreview=null;$('acquisition-trace').textContent='本任务诊断已关闭并清除；未删除岗位、报告或配额。';
}));
$('download-acquisition-trace').addEventListener('click',()=>act(async()=>{
 if(!tracePreview||tracePreview.id!==active())throw Error('请先预览当前任务诊断。');
 const u=URL.createObjectURL(new Blob([JSON.stringify(tracePreview.data,null,2)],{type:'application/json;charset=utf-8'}));
 const a=document.createElement('a');a.href=u;a.download='acquisition-diagnostic.json';a.click();
 setTimeout(()=>URL.revokeObjectURL(u),1000);
}));
$('task').addEventListener('change',()=>{tracePreview=null;$('acquisition-trace').textContent='任务已切换，请重新预览。';});

$('acquisition-backend').addEventListener('change',()=>{
 const native=$('acquisition-backend').value==='native';
 $('search-form').elements.native_consent.required=native;
 if(!native)$('search-form').elements.native_consent.checked=false;
});

$('forget-session').addEventListener('click',()=>{if(confirm('清除本工作区此平台保存的 Cookie，并关闭该平台采集会话；旧任务不再自动保存。不删除岗位、报告、个人证据或配额。是否继续？'))act(async()=>{await api('/api/guided/action',{id:active(),action:'forget_session',confirm:true});$('search-form').elements.persist_session.checked=false;});});
