"use strict";
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
const token = fragment.get("token") || sessionStorage.getItem("radar-session") || "";
const reportId = fragment.get("report") || "";
if (fragment.has("token")) sessionStorage.setItem("radar-session", token);
history.replaceState(null, "", location.pathname + "#report=" + encodeURIComponent(reportId));
$("back").href = "/#report=" + encodeURIComponent(reportId);
let state = null, currentId = "", requirements = [], dirty = false, pairDirty = false, busy = false, offsets = [];
let pairLeft = "", pairRight = "";
const relationLabels = {direct:"明确 AI 辅助编程",contextual:"AI 辅助工程上下文",role_related:"普通岗位能力"};
const strengthLabels = {required:"必须",expected:"职责 / 期望",preferred:"优先",not_required:"明确不要求",prohibited:"明确禁止",unspecified:"未明确强度"};
const reviewLabels = {pending:"草稿 / 未确认", assistant_draft:"助手草稿", human_confirmed:"本人已确认"};
const text = (id, value) => { $(id).textContent = value; };
async function request(path, data) {
  const body = JSON.stringify(data);
  if (new TextEncoder().encode(body).byteLength > 2000000)
    throw new Error("标注请求编码后超过 2 MB；请减少单次导入内容，原文件保持不变。");
  const response = await fetch(path, {method:"POST", cache:"no-store",
    headers:{"X-Radar-Token":token,"Content-Type":"application/json"}, body});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作失败");
  return result;
}
async function operation(action) {
  if (busy) return;
  busy = true;
  const controls = [...document.querySelectorAll("button,input,select,textarea")];
  const disabled = controls.map(control => control.disabled);
  controls.forEach(control => { control.disabled = true; });
  try { await action(); }
  catch (error) { text("notice", error.message || "操作失败，当前输入仍保留。"); }
  finally { busy = false; controls.forEach((control,index) => { control.disabled = disabled[index]; }); }
}
function mayDiscard() { return !(dirty || pairDirty) || window.confirm("当前 JD 或文档对有未保存的修改。放弃这些修改并继续？"); }
function source() { return state.corpus.find(row => row.record_id === currentId); }
function annotation() { return state.annotations.documents.find(row => row.record_id === currentId); }
function sourceValue(original) {
  // Map textarea UTF-16 positions, including newline normalization, to Python code points.
  const points = Array.from(original);
  let display = "", position = 0;
  offsets = [0];
  for (let index = 0; index < points.length;) {
    let value = points[index], width = 1;
    if (value === "\r") { value = "\n"; if (points[index + 1] === "\n") width = 2; }
    display += value;
    position += value.length;
    index += width;
    offsets[position] = index;
  }
  return display;
}
function renderRequirements() {
  $("requirements").replaceChildren();
  if (!requirements.length) { text("requirements", "尚未标注要求。没有要求也需在逐段核对后明确确认。"); return; }
  requirements.forEach((row, index) => {
    const card = document.createElement("div"); card.className = "requirement";
    const quote = document.createElement("p"); quote.className = "quote"; quote.textContent = row.quote;
    const details = document.createElement("p"); details.className = "muted";
    details.textContent = (state.labels.capabilities[row.capability]?.label || row.capability) +
      " · " + relationLabels[row.relation] + " · " + strengthLabels[row.strength] + " · 原文位置 " + row.start + "–" + row.end;
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "secondary";
    remove.textContent = "移除此条标注";
    remove.addEventListener("click", () => { requirements.splice(index,1); dirty = true; renderRequirements(); });
    card.append(quote,details,remove); $("requirements").append(card);
  });
}
function renderDocument(id) {
  currentId = id; $("document").value = id;
  const doc = source(), row = annotation();
  if (!doc || !row) { $("editor").hidden = true; return; }
  text("source-title", doc.title);
  text("source-meta", [doc.company,doc.location,doc.platform,doc.collected_at].filter(Boolean).join(" · "));
  text("document-info", reviewLabels[row.review_state] + " · " +
    (doc.extraction_eligible ? "可参与全文要求评估" : "不在原报告全文要求评估分母中") + " · " +
    (doc.classification_eligible ? "可参与岗位分类评估" : "不在岗位分类评估分母中"));
  $("source-text").value = sourceValue(doc.text);
  $("author").value = row.author;
  $("notes").value = row.notes;
  $("complete").checked = row.requirements_complete;
  $("roles-reviewed").checked = row.roles !== null;
  $("roles").replaceChildren();
  for (const [key,value] of Object.entries(state.labels.roles)) {
    const label = document.createElement("label"), input = document.createElement("input");
    input.type = "checkbox"; input.value = key; input.checked = (row.roles || []).includes(key);
    label.append(input,value.label); $("roles").append(label);
  }
  requirements = structuredClone(row.requirements);
  $("capability").replaceChildren(new Option("请选择能力",""));
  for (const [key,value] of Object.entries(state.labels.capabilities))
    $("capability").append(new Option(value.label,key));
  $("relation").value = ""; $("strength").value = ""; text("selection","");
  renderRequirements(); dirty = false;
}
function show(next, id = currentId, preserve = {}) {
  state = next;
  $("summary").hidden = false; $("editor").hidden = !state.corpus.length;
  text("source-info", (state.source_mode === "synthetic_demo" ? "合成演示" : "真实样本") +
    " · 报告 " + state.report_id + " · 标注版本 " + state.revision);
  const coverage = state.evaluation.coverage;
  const reviewed = state.annotations.documents.filter(row => row.review_state === "human_confirmed").length;
  text("coverage", "正文记录 " + state.corpus.length + " · 本人已确认 " + reviewed +
    " · 全文要求参与评估 " + coverage.extraction_evaluated_documents +
    " · 岗位分类参与评估 " + coverage.classification_evaluated_documents);
  $("metrics").replaceChildren();
  const scores = state.evaluation.extraction.semantic;
  for (const [label,value] of [["要求精确率",scores.precision],["要求召回率",scores.recall],
      ["岗位分类一致率",state.evaluation.classification.exact_match.accuracy],
      ["已评文档对",state.evaluation.deduplication.comparable_human_pairs]]) {
    const item = document.createElement("div"); item.className = "metric";
    const strong = document.createElement("strong");
    strong.textContent = value === null ? "待确认" : label === "已评文档对" ? String(value) : (value * 100).toFixed(1) + "%";
    item.append(strong,label); $("metrics").append(item);
  }
  $("document").replaceChildren();
  for (const doc of state.corpus) {
    const row = state.annotations.documents.find(item => item.record_id === doc.record_id);
    $("document").append(new Option((row.review_state === "human_confirmed" ? "✓ " : "○ ") + doc.title,doc.record_id));
  }
  if (state.corpus.length) {
    if (!(preserve.document && dirty)) renderDocument(state.corpus.some(row=>row.record_id===id) ? id : state.corpus[0].record_id);
    else $("document").value = currentId;
  } else text("notice","这份报告没有可复核记录；原报告保持不变。");
  if (!(preserve.pair && pairDirty)) renderPairs();
}
async function openReport() {
  const result = await request("/api/quality/open",{report_id:reportId});
  show(result); text("notice","已打开保存的质量复核；请选择 JD 独立标注。");
}
$("document").addEventListener("change", () => {
  if (dirty && !window.confirm("当前 JD 有未保存的修改，放弃并切换？")) { $("document").value = currentId; return; }
  renderDocument($("document").value);
});
$("editor").addEventListener("input", event => {
  if (event.target.id !== "document" && event.target.id !== "source-text") dirty = true;
});
$("add").addEventListener("click", () => {
  const input = $("source-text"), start = offsets[input.selectionStart], end = offsets[input.selectionEnd];
  if (!Number.isInteger(start) || !Number.isInteger(end) || start >= end) {
    text("notice","请先在原文中选中完整的要求文字。"); return;
  }
  const capability = $("capability").value, relation = $("relation").value, strength = $("strength").value;
  if (!capability || !relation || !strength) { text("notice","请选择能力、关系和强度。"); return; }
  if (requirements.some(row=>row.start===start && row.end===end && row.capability===capability)) {
    text("notice","同一原文位置和能力已标注，请先移除原条目再修改。"); return;
  }
  const quote = Array.from(source().text).slice(start,end).join("");
  requirements.push({start,end,quote,capability,relation,strength});
  dirty = true; renderRequirements(); text("notice","已添加要求，保存后生效。");
});
$("source-text").addEventListener("select", () => {
  const input = $("source-text"), start = offsets[input.selectionStart], end = offsets[input.selectionEnd];
  text("selection", Number.isInteger(start) && Number.isInteger(end) && start < end ?
    "已选择原文位置 " + start + "–" + end : "");
});
async function save(confirmed) {
  const author = $("author").value.trim();
  if (confirmed && !author) throw new Error("本人确认前请填写复核者。");
  const row = {...annotation(), author, review_state:confirmed ? "human_confirmed" : "pending",
    roles:$("roles-reviewed").checked ? [...$("roles").querySelectorAll("input:checked")].map(input=>input.value) : null,
    notes:$("notes").value, requirements_complete:$("complete").checked, requirements};
  const result = await request("/api/quality/save",{report_id:reportId,expected_revision:state.revision,
    document:row,confirm_human:confirmed});
  show(result, currentId, {pair:true});
  text("notice",confirmed ? "已保存本人确认的新版本；指标只统计适用样本。" : "草稿已保存；尚未计入质量指标。");
}
$("save").addEventListener("click",()=>operation(()=>save(false)));
$("confirm").addEventListener("click",()=>operation(()=>save(true)));
$("reload").addEventListener("click",()=>{if(mayDiscard()) operation(openReport);});
$("export").addEventListener("click",()=>operation(async()=>{
  if (dirty || pairDirty) throw new Error("请先保存当前 JD 和文档对的修改，再导出。");
  const response = await fetch("/api/quality/export/"+reportId+"/"+state.revision,{cache:"no-store",headers:{"X-Radar-Token":token}});
  if (!response.ok) { const result=await response.json(); throw new Error(result.error || "导出失败"); }
  const url=URL.createObjectURL(await response.blob()), link=document.createElement("a");
  link.href=url;link.download="jd-quality-"+reportId+"-v"+state.revision+".zip";link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);text("notice","已导出原文、标注和评估；请妥善保管本机材料。");
}));
$("import").addEventListener("click",()=>operation(async()=>{
  if (!mayDiscard()) return;
  const file=$("import-file").files[0];
  if (!file || file.size>2000000) throw new Error("请选择不超过 2 MB 的标注 JSON；发送前还会核对编码后的请求大小。");
  const annotations_json=await file.text();
  const result=await request("/api/quality/import_annotations",{report_id:reportId,
    expected_revision:state.revision,annotations_json,confirm_human:$("import-confirm").checked});
  show(result);$("import-confirm").checked=false;text("notice","离线标注已保存为新版本。");
}));

function documentLabel(doc) {
  return [doc.title, doc.company, doc.location, doc.record_id.slice(0,8)].filter(Boolean).join(" · ");
}
function renderPairForm(left, right) {
  pairLeft = left; pairRight = right;
  $("pair-left").value = left; $("pair-right").value = right;
  for (const side of ["left","right"]) {
    const doc = state.corpus.find(row => row.record_id === (side === "left" ? left : right));
    text("pair-"+side+"-text", doc?.text || "");
    text("pair-"+side+"-meta", doc ? [doc.company,doc.location,doc.platform,doc.url,doc.source_ref].filter(Boolean).join(" · ") : "");
  }
  const row = state.annotations.dedup_pairs.find(item =>
    (item.left === left && item.right === right) || (item.left === right && item.right === left));
  $("pair-author").value = row?.author || "";
  $("pair-notes").value = row?.notes || "";
  $("pair-decision").value = row?.same_job === true ? "same" : row?.same_job === false ? "different" : "";
  pairDirty = false;
}
function choosePair(left,right) {
  if (pairDirty && !window.confirm("当前文档对有未保存的修改，放弃并切换？")) {
    $("pair-left").value = pairLeft; $("pair-right").value = pairRight; return;
  }
  renderPairForm(left,right);
}
function renderPairs() {
  $("pairs").hidden = false;
  $("pair-content").hidden = state.corpus.length < 2;
  text("pair-availability", state.corpus.length < 2 ? "此报告少于两份记录，无法构成文档对。" : "先独立裁决，再查看指标。原预测不预选为答案。");
  for (const side of ["left","right"]) {
    $("pair-"+side).replaceChildren();
    for (const doc of state.corpus) $("pair-"+side).append(new Option(documentLabel(doc),doc.record_id));
  }
  const left = state.corpus.some(row=>row.record_id===pairLeft) ? pairLeft : state.corpus[0]?.record_id || "";
  const right = state.corpus.some(row=>row.record_id===pairRight) ? pairRight : state.corpus[1]?.record_id || "";
  renderPairForm(left,right);
  $("pair-list").replaceChildren();
  for (const row of state.annotations.dedup_pairs) {
    const label = id => documentLabel(state.corpus.find(doc=>doc.record_id===id));
    const block = document.createElement("div"); block.className = "requirement";
    const description = document.createElement("p"); description.className = "quote";
    description.textContent = label(row.left) + " ↔ " + label(row.right) + " · " + reviewLabels[row.review_state] +
      " · " + (row.same_job === true ? "同一岗位" : row.same_job === false ? "不同岗位" : "尚未裁决");
    const button = document.createElement("button"); button.type = "button"; button.className = "secondary";
    button.textContent = "查看或修改此文档对";
    button.addEventListener("click", () => choosePair(row.left,row.right));
    block.append(description,button); $("pair-list").append(block);
  }
  if (!state.annotations.dedup_pairs.length) text("pair-list","尚未保存文档对；不会自动创建人工标签。");
}
for (const side of ["left","right"])
  $("pair-"+side).addEventListener("change",()=>choosePair($("pair-left").value,$("pair-right").value));
for (const id of ["pair-author","pair-notes","pair-decision"])
  $(id).addEventListener("input",()=>{pairDirty=true;});
async function savePair(confirmed) {
  if (!pairLeft || !pairRight || pairLeft === pairRight) throw new Error("请选择两份不同的 JD。");
  const author = $("pair-author").value.trim(), decision = $("pair-decision").value;
  if (confirmed && (!author || !decision)) throw new Error("本人确认前请填写复核者并明确裁决。");
  const pair = {left:pairLeft,right:pairRight,same_job:decision ? decision === "same" : null,
    author,review_state:confirmed ? "human_confirmed" : "pending",reviewed_at:"",notes:$("pair-notes").value};
  const result = await request("/api/quality/save_pair",{report_id:reportId,expected_revision:state.revision,pair,confirm_human:confirmed});
  show(result,currentId,{document:true});
  text("notice",confirmed ? "文档对已确认；指标只统计适用的完整正文对。" : "文档对草稿已保存，尚未计入指标。");
}
$("pair-save").addEventListener("click",()=>operation(()=>savePair(false)));
$("pair-confirm").addEventListener("click",()=>operation(()=>savePair(true)));

window.addEventListener("beforeunload",event=>{if(dirty || pairDirty){event.preventDefault();event.returnValue="";}});
if (!/^[a-f0-9]{32}$/.test(reportId)) text("notice","请从工作台的具体报告进入质量复核。");
else operation(openReport);
