"use strict";
const $ = id => document.getElementById(id);
const token = sessionStorage.getItem("radar-session") || "";
let profile = null, page = 0, total = 0, runId = "", activeCollection = "", looping = false, collecting = false;
let collectionGuide = null, initializing = true;
let collectionMode = "", backgroundCollection = false, foreignCollection = false;
const linkedReport = new URLSearchParams(location.hash.slice(1)).get("report");
let linkedReportLoaded = false;
let selected = new Map(), metrics = [], busy = false, snapshotCapabilities = {};
const note = text => { $("notice").textContent = text; };
async function api(path, data={}) {
  const response = await fetch(path, {method:"POST", cache:"no-store", headers:{"Content-Type":"application/json","X-Radar-Token":token}, body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function updateButtons() {
  const locked=initializing||busy||collecting;
  $("collection-controls").disabled=initializing;
  document.querySelectorAll("button").forEach(b=>{b.disabled=locked;});
  $("collect-pause").disabled=!collecting||foreignCollection;
  $("prev-page").disabled=locked||page===0;
  $("next-page").disabled=locked||(page+1)*50>=total;
  if (collectionGuide) collectionGuide.sync(locked);
}
async function act(fn) {
  if (initializing) { note("正在读取采集配置与后台状态，请稍候。"); return; }
  if (busy || collecting) { note("已有操作执行中，请先暂停连续采集或完成当前操作。"); return; }
  busy = true; updateButtons();
  try { await fn(); } catch(error) { note(error.message || "操作失败"); } finally { busy=false; updateButtons(); }
}
function options(select, values, value="") {
  select.replaceChildren();
  for (const [key,label] of Object.entries(values)) { const o=document.createElement("option");o.value=key;o.textContent=label;select.append(o); }
  if ([...select.options].some(o=>o.value===value)) select.value=value;
}
function checks(id, values, checked=[]) {
  $(id).replaceChildren();
  for (const [key,label] of Object.entries(values)) { const l=document.createElement("label"),i=document.createElement("input");i.type="checkbox";i.value=key;i.checked=checked.includes(key);l.append(i,label);$(id).append(l); }
}
const chosen=id=>[...$(id).querySelectorAll("input:checked")].map(i=>i.value);
function setChecks(id, values) { for(const i of $(id).querySelectorAll("input"))i.checked=values.includes(i.value); }
function text(tag, value) { const e=document.createElement(tag); e.textContent=value;return e; }
function button(label, fn) {const b=text("button",label); b.type="button";b.disabled=initializing||busy||collecting;b.addEventListener("click",()=>act(fn));return b;}
function table(id, headers, rows) {
  const t=document.createElement("table"),head=document.createElement("tr");headers.forEach(h=>head.append(text("th",h)));t.append(head);
  rows.forEach(row=>{const tr=document.createElement("tr");row.forEach(v=>tr.append(text("td",String(v??""))));t.append(tr);});$(id).replaceChildren(t);
}
async function download(path, filename) {
  const response=await fetch(path,{headers:{"X-Radar-Token":token},cache:"no-store"});
  if(!response.ok)throw new Error((await response.json()).error||"下载失败");
  const url=URL.createObjectURL(await response.blob()),a=document.createElement("a");a.href=url;a.download=filename;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);note(`已请求下载 ${filename}。`);
}
function reportDownloads(report, target) {
  $(target).replaceChildren();
  for(const name of report.files) $(target).append(button(name,()=>download(`/api/download/${report.id}/${encodeURIComponent(name)}`,name)));
}
async function refreshProfile(newState) {
  profile=newState||await api("/api/evidence/state");
  $("revision").textContent=`个人证据版本 ${profile.revision} · 已保存证据 ${profile.evidence.length} · 已复核条目 ${Object.keys(profile.reviews).length}`;
  const old=$("source-run").value;
  const runOptions=Object.fromEntries(profile.runs.map(r=>[r.id,`${r.created_at} · ${r.stats.requirement_rows} 条 · ${r.id.slice(0,8)}`]));
  if(old===linkedReport && linkedReportLoaded && runId===old && !runOptions[old]) runOptions[old]=`指定历史报告 · ${old.slice(0,8)}`;
  options($("source-run"),runOptions,old);
  options($("artifact-select"),{"":"无附件 / 使用外部引用",...Object.fromEntries(profile.artifacts.map(a=>[a.id,`${a.name} · ${a.size} 字节 · ${a.id.slice(0,12)}`]))},$("artifact-select").value);
  if(!$("capabilities").childElementCount)checks("capabilities",profile.capabilities);
  if(!$("metric-id").options.length)options($("metric-id"),Object.fromEntries(profile.metrics.map(m=>[m.metric_id,`${m.label} (${m.unit})`])));
  if(!$("evidence-form").elements.name.value)$("evidence-form").elements.name.value=profile.name;
  $("revision-history").textContent=profile.history.map(h=>`${h.revision} · ${h.created_at} · ${h.action}`).join("\n")||"尚无修改记录。";
  $("saved-evidence").replaceChildren();
  for(const e of profile.evidence){const card=document.createElement("div");card.className="card";card.append(text("h3",`${e.project} · ${e.scope} · ${e.review_status}`),text("p",e.contribution),text("p",`${e.requirement_ids.length} 个精确要求映射 · ${e.metrics.length} 项指标`));
    card.append(button("编辑证据",()=>editEvidence(e)),button("撤回证据",async()=>{if(!confirm("从当前资料撤回？旧报告和审计历史仍保留。"))return;await refreshProfile(await api("/api/evidence/remove",{expected_revision:profile.revision,evidence_id:e.evidence_id}));note("当前资料已撤回该证据，历史仍保留。");}));
    if(e.artifact_id)card.append(button("下载原始附件",()=>download(`/api/evidence/attachment/${e.artifact_id}`,profile.artifacts.find(a=>a.id===e.artifact_id)?.name||e.artifact_id)));
    $("saved-evidence").append(card);}
  metricContract();
}
function mappingCount(){ $("selection-count").textContent=`已选择 ${selected.size} 条要求（跨页保留）`;$("evidence-mapping").textContent=`当前证据映射：${selected.size} 条；来源报告 ${runId.slice(0,8)||"未选择"}`; }
async function loadRequirements(reset=false) {
  const nextRun=$("source-run").value;
  if(!nextRun)throw new Error("先在基础工作台生成真实输入报告，或完成一次真实数据采集。");
  if(reset||nextRun!==runId){page=0;selected=new Map();}
  runId=nextRun;
  const result=await api("/api/evidence/catalogue",{run_id:runId,query:$("requirement-query").value,page});total=result.total;
  snapshotCapabilities=result.capabilities;
  checks("capabilities",snapshotCapabilities,chosen("capabilities"));
  // Do not silently refresh the optimistic revision while the user is editing.
  $("requirement-list").replaceChildren();
  for(const row of result.rows){const card=document.createElement("div");card.className="card";const label=document.createElement("label"),check=document.createElement("input");check.type="checkbox";check.checked=selected.has(row.requirement_id);check.setAttribute("data-rid",row.requirement_id);
    check.addEventListener("change",()=>{if(check.checked){selected.set(row.requirement_id,row.capability);const cap=[...$("capabilities").querySelectorAll("input")].find(i=>i.value===row.capability);if(cap)cap.checked=true;}else selected.delete(row.requirement_id);mappingCount();});
    label.append(check,`${row.title} · ${snapshotCapabilities[row.capability]||row.capability} · ${row.strength} · ${row.evidence_level}`);card.append(label,text("p",row.quote));card.append(text("small",`${row.requirement_id} · ${row.relation} · 复核 ${row.saved_review?.decision||row.review_status}`));
    const details=document.createElement("details");details.append(text("summary","完整职位原文与引用位置"));const pre=document.createElement("pre");const characters=Array.from(row.source_text);pre.append(document.createTextNode(characters.slice(0,row.start).join("")),text("mark",row.quote),document.createTextNode(characters.slice(row.end).join("")));details.append(pre,text("p",row.url));card.append(details);
    card.append(button("复核此条",async()=>{const f=$("review-form");f.elements.requirement_id.value=row.requirement_id;f.elements.decision.value=row.saved_review?.decision||"approve";f.elements.reviewer.value=row.saved_review?.reviewer||"";f.elements.reason.value=row.saved_review?.reason||"";$("review-source").textContent=row.source_text;$("review-panel").open=true;$("review-panel").scrollIntoView({block:"center"});}));$("requirement-list").append(card);}
  $("pagination").textContent=`第 ${page+1} 页 / ${Math.max(1,Math.ceil(total/50))} 页，共 ${total} 条`;
  updateButtons();mappingCount();
}
function metricData(){const f=$("metric-form").elements,d={metric_id:f.metric_id.value,current:Number(f.current.value),sample_size:Number(f.sample_size.value),window:f.window.value,comparison_basis:f.comparison_basis.value};
  if(!f.current.value||!f.sample_size.value)throw new Error("请填写当前值和样本量（0 必须明确填写）。");
  if(f.baseline.value!=="")Object.assign(d,{baseline:Number(f.baseline.value),baseline_sample_size:Number(f.baseline_sample_size.value),baseline_window:f.baseline_window.value});return d;}
function metricContract(){const m=profile?.metrics.find(m=>m.metric_id===$("metric-id").value);$("metric-contract").textContent=m?`${m.formula}；${m.measurement_contract}`:"";}
function renderMetrics(){ $("metric-list").replaceChildren();metrics.forEach((m,i)=>{const card=document.createElement("div");card.className="card";card.append(text("p",`${m.metric_id}：基线 ${m.baseline??"未提供"} → 当前 ${m.current}；样本 ${m.sample_size}；窗口 ${m.window}`));card.append(button("移除此指标",async()=>{metrics.splice(i,1);renderMetrics();}));$("metric-list").append(card);}); }
async function editEvidence(e){
  const f=$("evidence-form").elements;for(const key of ["evidence_id","project","contribution","scope","review_status","artifact_id","external_url","reviewer"])f[key].value=e[key]||"";f.name.value=profile.name;f.attested.checked=false;
  if(![...$("source-run").options].some(o=>o.value===e.source_run_id)){$("source-run").append(new Option(e.source_run_id,e.source_run_id));}
  $("source-run").value=e.source_run_id;runId=e.source_run_id;page=0;selected=new Map(e.requirement_ids.map(rid=>[rid,""]));metrics=structuredClone(e.metrics);setChecks("capabilities",e.capabilities);renderMetrics();await loadRequirements(false);setChecks("capabilities",e.capabilities);note("正在编辑已有证据；修改后需重新确认真实性。");}
$("load-requirements").onclick=()=>act(()=>loadRequirements(true));
$("reload-state").onclick=()=>act(async()=>{await refreshProfile();note("已读取最新版本；请核对未保存的编辑再提交。");});
$("prev-page").onclick=()=>act(async()=>{if(page>0){page--;await loadRequirements();}});
$("next-page").onclick=()=>act(async()=>{if((page+1)*50<total){page++;await loadRequirements();}});
$("review-form").onsubmit=event=>{event.preventDefault();act(async()=>{const d=Object.fromEntries(new FormData(event.target));await refreshProfile(await api("/api/evidence/review",{...d,run_id:runId,expected_revision:profile.revision}));await loadRequirements();note("复核已保存。摘要仍是摘要，不会因此成为正文。");});};
$("upload-form").onsubmit=event=>{event.preventDefault();act(async()=>{const file=$("evidence-file").files[0];if(!file||file.size>1000000)throw new Error("请选择不超过 1 MB 的附件。");const bytes=new Uint8Array(await file.arrayBuffer());let binary="";for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));const result=await api("/api/evidence/upload",{name:file.name,content_base64:btoa(binary),rights_confirmed:event.target.elements.rights_confirmed.checked});await refreshProfile();$("artifact-select").value=result.artifact_id;$("evidence-form").elements.external_url.value="";$("upload-result").textContent=`已上传 ${result.size} 字节；SHA-256 ${result.sha256}`;note("附件已保存，可在证据表单中使用。");});};
$("metric-id").onchange=metricContract;
$("metric-preview").onclick=()=>act(async()=>{$("metric-preview-text").textContent=(await api("/api/evidence/metric",metricData())).description;});
$("metric-form").onsubmit=event=>{event.preventDefault();act(async()=>{const d=metricData(),result=await api("/api/evidence/metric",d);metrics.push(d);renderMetrics();$("metric-preview-text").textContent=result.description;note("指标已添加到当前编辑表单；需保存证据才会持久化。");});};
$("evidence-form").onsubmit=event=>{event.preventDefault();act(async()=>{if(runId!==$("source-run").value)throw new Error("请先加载当前选择报告的要求，防止跨报告误映射。");const d=Object.fromEntries(new FormData(event.target));Object.assign(d,{run_id:runId,expected_revision:profile.revision,metrics,requirement_ids:[...selected.keys()],capabilities:chosen("capabilities"),attested:event.target.elements.attested.checked});await refreshProfile(await api("/api/evidence/save",d));event.target.elements.evidence_id.value=profile.evidence[profile.evidence.length-1].evidence_id;note("个人证据已保存。重新生成报告后才会体现本次修改。");});};
$("new-evidence").onclick=()=>act(async()=>{$("evidence-form").reset();$("evidence-form").elements.evidence_id.value="";$("evidence-form").elements.name.value=profile.name;metrics=[];selected=new Map();renderMetrics();setChecks("capabilities",[]);mappingCount();if(runId)await loadRequirements();});
$("generate").onclick=()=>act(async()=>{const result=await api("/api/evidence/generate",{run_id:$("source-run").value,expected_revision:profile.revision});$("generated-summary").textContent=`个人报告 ${result.id} · 版本 ${profile.revision}。证据映射覆盖率不是录用概率。`;$("generated-descriptions").textContent=result.descriptions;
  table("generated-coverage",["岗位","人工声明映射覆盖率","说明"],result.coverage.map(r=>[r.title,r.evidence_mapping_coverage,r.note]));table("generated-matrix",["要求ID","状态","证据ID","说明"],result.matrix.map(r=>[r.requirement_id,r.status,r.evidence_ids,r.note]));reportDownloads(result,"downloads");await refreshProfile();note("个人报告已生成，旧报告保持不变。");});
$("export-evidence").onclick=()=>act(()=>download("/api/evidence/export","personal-evidence.zip"));
async function refreshCollections(){const result=await api("/api/collection/list");options($("collect-history"),Object.fromEntries(result.runs.map(s=>[s.id,`${s.updated_at} · ${s.mode} · ${s.status} · ${s.id.slice(0,8)}`])),activeCollection);}
function showCollection(s){
  activeCollection=s.id;
  collectionMode=s.mode;
  $("collect-progress").textContent=`任务 ${s.id.slice(0,8)} · ${s.route_label||s.mode} · ${s.status} · 阶段 ${s.phase} · 正文尝试 ${s.detail_attempts}/${s.detail_budget}`;
  table("collect-summary",["平台","搜索请求","正文状态","平台接入认证"],Object.entries(s.platform_summary).map(([k,v])=>[k,v.queries_attempted,JSON.stringify(v.detail_outcomes),"未认证；仅显示本次观测"]));
  $("collect-json").textContent=JSON.stringify(s,null,2);
  const root=$("collect-result");root.replaceChildren();
  root.append(text("p",s.user_summary||"请核对每条实际结果。"));
  for(const row of s.category_outcomes||[]){
    const pageLabel=Number.isInteger(row.page_snapshot?.page)?`第 ${row.page_snapshot.page+1} 页`:'已保存分类名单';
    const duplicates=(row.candidates||[]).filter(item=>item.status==='previous_page_duplicate').length;
    root.append(text("p",`${pageLabel} · ${row.status_message||row.status}；主列表卡片 ${row.card_count??"尚未确认"} 条，已选 ${(row.selected_positions||[]).length} 条。${duplicates?` 前页已出现 ${duplicates} 项，未重复请求。`:''}`));
  }
  if(s.mode==="liepin_category"&&["completed","needs_attention","empty"].includes(s.status)){
    if(s.details.some(row=>["rate_wait","publisher_wait","hourly_limit","daily_limit","cooldown"].includes(row.status))){
      const recovery=text("div","");recovery.className="guide-box";root.append(recovery);
      recovery.append(button("预览因等待未完成的正文（不联网）",async()=>{
        const plan=await api("/api/collection/category_recovery_preview",{id:s.id});
        recovery.replaceChildren(text("p",plan.notice));
        recovery.append(text("p",`保留原成功 ${plan.inherited_success_count} 条；本次最多 ${plan.selection_limit} 次正文尝试，原预算不退款。`));
        if(plan.existing_task_id){
          recovery.append(button("打开已保存的恢复任务",async()=>{
            const saved=await api("/api/collection/status",{id:plan.existing_task_id});
            collectionGuide.selectMode(saved.mode);showCollection(saved);await refreshCollections();
          }));return;
        }
        for(const item of plan.items)recovery.append(text("p",`名单第 ${item.position} 项：${item.title} ${item.url}`));
        recovery.append(text("p","沿用原用途与许可范围："+plan.rights_note));
        recovery.append(button("确认保存恢复任务（暂不联网）",async()=>{
          const result=await api("/api/collection/category_recovery_start",{id:plan.id,fingerprint:plan.fingerprint,consent:true});
          collectionGuide.selectMode(result.task.mode);showCollection(result.task);await refreshCollections();
          note("恢复任务已保存。等待原因解除后点击继续；程序会重新检查共享额度，尚未访问网站。");
        }));
      }));
    }
    const next=text("div","");next.className="guide-box";root.append(next);
    next.append(button("预览这份名单的下一批（不联网）",async()=>{
      const plan=await api("/api/collection/category_next_preview",{id:s.id});
      next.replaceChildren(text("p",plan.notice));
      next.append(text("p",plan.source_time_known?"原名单读取时间："+plan.snapshot_observed_at:
        "旧名单未记录准确读取时刻；来源任务建立于："+plan.source_task_created_at));
      if(plan.existing_task_id){
        next.append(button("打开已保存的下一批",async()=>{
          const saved=await api("/api/collection/status",{id:plan.existing_task_id});
          collectionGuide.selectMode(saved.mode);showCollection(saved);await refreshCollections();
        }));return;
      }
      if(plan.exhausted){next.append(text("p","这份名单已全部选择完毕；不代表网站或市场上没有其他岗位。"));return;}
      next.append(text("p",`本批 ${plan.items.length} 项，最多 ${plan.selection_limit} 次正文尝试；不重新读取分类页，原批预算不退款。`));
      for(const item of plan.items)next.append(text("p",`名单第 ${item.position} 项：${item.title||"无法确认的卡片（会保留失败）"} ${item.url}`));
      next.append(text("p","沿用原用途与许可范围："+plan.rights_note));
      const launch=text("button","确认采集这批职位");launch.type="button";
      launch.onclick=async()=>{
        let created=false;
        await act(async()=>{
          const result=await api("/api/collection/category_next_start",{id:plan.id,fingerprint:plan.fingerprint,consent:true});
          collectionGuide.selectMode(result.task.mode);showCollection(result.task);
          await refreshCollections();created=result.created;
        });
        if(created)await continueCollection();
      };
      next.append(launch);
    }));
    const pageBox=text("div","");pageBox.className="guide-box";root.append(pageBox);
    pageBox.append(button("查看平台下一页（不联网）",async()=>{
      const plan=await api("/api/collection/category_page_preview",{id:s.id});
      pageBox.replaceChildren(text("p",plan.notice));
      if(!plan.can_start)return;
      pageBox.append(text("p",`第 ${plan.current_page} 页 → 第 ${plan.next_page} 页：${plan.next_url}`));
      if(plan.existing_task_id){
        pageBox.append(button("打开已保存的下一页任务",async()=>{
          const saved=await api("/api/collection/status",{id:plan.existing_task_id});
          collectionGuide.selectMode(saved.mode);showCollection(saved);await refreshCollections();
        }));return;
      }
      pageBox.append(text("p","沿用原用途与许可范围："+plan.rights_note));
      const launch=text("button",`确认读取第 ${plan.next_page} 页并采集最多 ${plan.selection_limit} 条`);launch.type="button";
      launch.onclick=async()=>{
        let created=false;
        await act(async()=>{
          const result=await api("/api/collection/category_page_start",{id:plan.id,fingerprint:plan.fingerprint,consent:true});
          collectionGuide.selectMode(result.task.mode);showCollection(result.task);await refreshCollections();created=result.created;
        });
        if(created)await continueCollection();
      };
      pageBox.append(launch);
    }));
  }
  for(const row of s.details){
    const card=text("div","");card.className="card";
    card.append(text("strong",row.status_message||row.status),text("p",row.url),text("p",row.next_action||""));
    if(row.final_url)card.append(text("p","最终正文地址："+row.final_url));
    if(row.fetch_diagnostic){const detail=document.createElement("details");detail.append(text("summary","本条跳转与请求诊断（已移除目标参数）"),text("pre",JSON.stringify(row.fetch_diagnostic,null,2)));card.append(detail);}
    root.append(card);
  }
  if(["urls","search"].includes(s.mode)&&s.details.some(d=>!["ok","fresh_reused","pending","budget_skipped"].includes(d.status))){
    const handoff=text("div","");root.append(handoff);
    handoff.append(button("将未完成链接转交浏览器（先预览，不联网）",async()=>{
      const plan=await api("/api/collection/handoff_preview",{id:s.id});handoff.replaceChildren(text("p",plan.notice));
      handoff.append(text("p",`原HTTP正文尝试 ${plan.budget.used}/${plan.budget.limit}，剩余 ${plan.budget.remaining}；已成功、预算未执行、明确拒绝和限流条目不会自动转交。`));
      handoff.append(text("p","沿用原授权范围："+plan.rights_note));
      if(!plan.groups.length)handoff.append(text("p","没有符合转交条件的链接。请核对访问限制；程序不会换路线绕过拒绝。"));
      for(const group of plan.groups){const section=text("div","");section.className="card";section.append(text("strong",group.label));
        if(group.existing_url){const link=text("a","继续已保存的浏览器任务（不重复创建）");link.href=group.existing_url;section.append(link);handoff.append(section);continue;}
        const choices=[];for(const item of group.items){const label=text("label","");const check=document.createElement("input");check.type="checkbox";check.checked=choices.length<Math.min(5,plan.max_transfer);choices.push({check,index:item.index});label.append(check,document.createTextNode(item.url+" · "+item.reason));section.append(label);}
        section.append(button("确认转交并继续",async()=>{const indices=choices.filter(x=>x.check.checked).map(x=>x.index);
          if(!indices.length||indices.length>plan.max_transfer)throw Error(`请选择1至${plan.max_transfer}条岗位。`);
          if(!window.confirm(`沿用原授权范围：${plan.rights_note}\n将 ${indices.length} 条未成功链接交给浏览器，另授权最多 ${indices.length} 条正文尝试。原HTTP预算不退款，不传递账号、Cookie或密钥。仍遵守发布方规则；需要登录时由你在平台原生页完成。是否继续？`))return;
          const result=await api("/api/collection/handoff_start",{id:plan.id,fingerprint:plan.fingerprint,platform:group.platform,indices,consent:true});location.assign(result.url);
        }));handoff.append(section);}
    }));
  }
  if(s.report_id)root.append(button("下载采集审计",()=>download(`/api/download/${s.report_id}/collection_manifest.json`,"collection_manifest.json")),button("下载逐条要求",()=>download(`/api/download/${s.report_id}/requirements_zh.csv`,"requirements_zh.csv")));
}
async function watchCategory(initial){
  collecting=true;backgroundCollection=true;looping=true;
  let current=initial;
  try{
    while(true){
      foreignCollection=current.owned_elsewhere;
      if(current.task){collectionGuide.selectMode(current.task.mode);showCollection(current.task);}
      updateButtons();
      $("collect-background-note").textContent=current.active?
        (foreignCollection?"此批由另一个本机服务执行；可查看进度，请回到原服务暂停。":
        "本机正在完成已确认的当前批次。可以关闭或刷新网页；退出工作台应用会停止后续步骤，重启后须点击继续。"):
        ({conditions_changed:"任务条件或网络设置已变化，后台已停止。请核对后再继续。",
          interrupted_uncertain:"上次执行结果未知。保留检查点，重新打开工作台后核对中断条目；不会自动重放。",
          execution_failed:"后台执行中止。已保存的正文和配额保留，请核对任务记录。",
          application_closed:"工作台已退出，当前批次保留；重新打开后可确认继续。",
          user_pause:"后台已暂停，当前请求已处理并保存；继续只执行未完成步骤。",
          finished:"当前批次已结束，正文和报告已保存。"}[current.code]||"");
      if(!current.active){await refreshProfile();break;}
      await new Promise(resolve=>setTimeout(resolve,250));
      current=await api("/api/collection/background/state");
    }
  }catch(error){note(error.message+"；后台可能仍在执行，请重新打开页面查看进度。");}
  finally{looping=false;collecting=false;backgroundCollection=false;foreignCollection=false;try{await refreshCollections();}finally{updateButtons();}}
}
async function continueCollection(){if(initializing||busy||collecting)return;
  if(collectionMode==="liepin_category"){
    collecting=true;updateButtons();
    try{const started=await api("/api/collection/background/start",{id:activeCollection,consent:true});await watchCategory(started);}
    catch(error){collecting=false;note(error.message);updateButtons();}return;
  }
  $("collect-background-note").textContent="";
  collecting=true;looping=true;updateButtons();try{while(looping){const s=await api("/api/collection/step",{id:activeCollection,api_key:$("collect-form").elements.api_key.value});showCollection(s);if(["completed","needs_attention","empty"].includes(s.status)){looping=false;$("collect-form").elements.api_key.value="";note(`采集结束：${s.status}。请核对各平台失败和预算跳过项。`);await refreshProfile();break;}await new Promise(resolve=>setTimeout(resolve,30));}}catch(error){note(error.message);looping=false;}finally{looping=false;try{await refreshCollections();}catch(error){note(error.message);}finally{collecting=false;updateButtons();}}}
$("collect-form").onsubmit=async event=>{event.preventDefault();if(initializing||collecting||busy)return;let created=false;await act(async()=>{const d=collectionGuide.data();const check=await api("/api/collection/preview",d);collectionGuide.render(check);if(!check.ready)return;delete d.api_key;showCollection(await api("/api/collection/start",d));await refreshCollections();created=true;});if(created)await continueCollection();};
$("collect-pause").onclick=async()=>{if(backgroundCollection){try{await api("/api/collection/background/pause",{id:activeCollection});}catch(error){note(error.message);return;}}else{looping=false;}note("已请求暂停；当前请求结束后不再发出下一次请求。任务进度已保留。");};
$("collect-resume").onclick=async()=>{
  if(initializing||collecting||busy)return;
  activeCollection=$("collect-history").value||activeCollection;
  if(!activeCollection){note("请先创建或选择一个任务。");return;}
  let resume=false;
  await act(async()=>{
    const saved=await api("/api/collection/status",{id:activeCollection});
    const switched=$("collect-form").elements.mode.value!==saved.mode;
    collectionGuide.selectMode(saved.mode);
    showCollection(saved);
    if(["completed","needs_attention","empty"].includes(saved.status)){
      note("所选任务已经结束；继续不会重试已结束的请求。需要重试时请核对原因后新建任务。");
      return;
    }
    const key=$("collect-form").elements.api_key.value.trim();
    // Credentials are required by the current phase, not by the original route.
    if(saved.phase==="search"&&!key&&!collectionGuide.keyConfigured)
      throw new Error("所选任务仍在搜索阶段：请先填入自己的 Brave Key，再点击继续；任务尚未执行。");
    if(saved.phase==="feed"&&switched){
      note("已切换到数据源任务，尚未发出请求。请补填该数据源 Token 后再点继续；只有提供方明确无需 Token 时才确认无 Token 继续。");
      return;
    }
    if(saved.phase==="feed"&&!key&&!window.confirm("未填写数据源 Token。只有提供方明确允许无需 Token 访问时，才确认无 Token 继续；否则点取消，先填 Token。")){
      note("已取消继续；未发出采集请求，未消耗任务预算。请补填数据源 Token。");
      return;
    }
    resume=true;
  });
  if(resume)await continueCollection();
};
$("collect-load").onclick=()=>act(async()=>showCollection(await api("/api/collection/status",{id:$("collect-history").value})));
$("collect-refresh").onclick=()=>act(refreshCollections);
$("platform-form").onsubmit=event=>{event.preventDefault();act(async()=>{const result=await api("/api/collection/register",Object.fromEntries(new FormData(event.target)));await init();note(result.message);});};
async function init(){if(!token)throw new Error("请先从启动器地址打开基础工作台，再进入本页面。");const response=await fetch("/api/status",{headers:{"X-Radar-Token":token},cache:"no-store"});const s=await response.json();if(!response.ok)throw new Error(s.error);const platforms=Object.fromEntries(Object.entries(s.platforms).map(([k,v])=>[k,`${v.label} (${v.domains.join(",")})`]));checks("collect-platforms",platforms,Object.keys(platforms));checks("collect-roles",s.roles,Object.keys(s.roles));checks("collect-permits",platforms,[]);if(!collectionGuide)collectionGuide=new window.CollectionGuide({api,note,act,chosen,setChecks,platforms:s.platforms,keyConfigured:s.brave_key_configured});await refreshProfile();await refreshCollections();mappingCount();updateButtons();await openLinkedReport();}
async function openLinkedReport() {
  if (linkedReport === null || linkedReportLoaded) return;
  linkedReportLoaded = true;
  if (!/^[a-f0-9]{32}$/.test(linkedReport)) {
    $('source-run').value = ''; throw new Error('报告链接无效，未自动加载其他报告。');
  }
  const response = await fetch('/api/report/' + linkedReport, {headers:{'X-Radar-Token':token},cache:'no-store'});
  const report = await response.json();
  if (!response.ok || report.manifest?.mode !== 'real_sample') {
    $('source-run').value = ''; throw new Error('原报告不存在或属于合成演示，未改用其他真实报告。');
  }
  if (![...$('source-run').options].some(o=>o.value===linkedReport)) {
    const option = document.createElement('option'); option.value = linkedReport;
    option.textContent = `指定历史报告 · ${linkedReport.slice(0,8)}`; $('source-run').append(option);
  }
  $('source-run').value = linkedReport;
  try { await loadRequirements(true); }
  catch (error) { $('source-run').value=''; runId=''; throw error; }
  let back = $('research-return');
  if (!back) { back=document.createElement('a'); back.id='research-return'; back.textContent='返回这份岗位研究结论'; $('source-run').closest('section').prepend(back); }
  back.href='/#report='+linkedReport;
  $('source-run').closest('section').scrollIntoView({block:'start'});
  note('已加载来自研究结果的同一份报告。请先复核原文，再用本人实际项目举证；没有自动批准或修改个人资料。');
}
updateButtons();
note("正在读取采集配置与后台状态，请稍候。");
init().then(async()=>{
  if(location.hash==="#liepin-category")collectionGuide.preset("liepin_category");
  if(location.hash==="#liepin-algorithm")collectionGuide.preset("liepin_category", "algorithm");
  const background=await api("/api/collection/background/state");
  initializing=false;updateButtons();
  if(!location.hash)note("");
  if(background.active||(background.task&&background.status==="paused"))await watchCategory(background);
}).catch(error=>note(error.message+"；请刷新页面重新读取配置与后台状态。"));
