"use strict";
// One report-scoped, read-only viewer shared by the research and evidence pages.
window.createReportSources = function(root, api) {
  let runId = "", generation = 0, page = 0, total = 0, loading = false;
  function text(tag, value) {
    const node = document.createElement(tag); node.textContent = value; return node;
  }
  function control(label, fn) {
    const button = text("button", label); button.type = "button";
    button.addEventListener("click", fn); return button;
  }
  const heading = text("h3", "本报告保存的岗位原文");
  const note = text("p", "完整正文与摘要分别标明；这里保留生成这份报告时保存的内容。");
  const status = text("p", ""); status.setAttribute("role", "status");
  const list = text("div", ""); list.dataset.role = "source-list";
  const pagination = text("p", "");
  const show = control("查看本报告的岗位原文", () => loadPage(0));
  const previous = control("上一页原文", () => loadPage(page - 1));
  const next = control("下一页原文", () => loadPage(page + 1));
  const navigation = text("div", ""); navigation.append(previous, next);
  root.replaceChildren(heading, note, show, status, list, pagination, navigation);
  function buttons() {
    show.disabled = loading || !runId;
    previous.disabled = loading || page === 0;
    next.disabled = loading || (page + 1) * 20 >= total;
  }
  function current(id, version) { return id === runId && version === generation; }
  async function loadBody(details, output, row, id, version) {
    if (!details.open || details.dataset.loaded || details.dataset.loading) return;
    details.dataset.loading = "true"; output.textContent = "正在读取这份报告保存的原文…";
    try {
      const result = await api("/api/evidence/source", {run_id:id, record_id:row.record_id});
      if (!current(id, version) || !root.contains(details)) return;
      if (result.run_id !== id || result.record?.record_id !== row.record_id ||
          typeof result.record.text !== "string") throw new Error("原文与所选报告或岗位不一致。");
      output.textContent = result.record.text; details.dataset.loaded = "true";
    } catch (error) {
      if (current(id, version) && root.contains(details)) output.textContent = error.message || "原文读取失败，请重新展开。";
    } finally { delete details.dataset.loading; }
  }
  async function loadPage(targetPage) {
    if (!runId || loading || targetPage < 0) return;
    const id = runId, version = ++generation;
    loading = true; buttons(); status.textContent = "正在读取本报告的岗位清单…";
    list.replaceChildren(); navigation.hidden = true; pagination.textContent = "";
    try {
      const result = await api("/api/evidence/sources", {run_id:id, page:targetPage});
      if (!current(id, version)) return;
      if (result.run_id !== id || result.page !== targetPage || result.page_size !== 20 ||
          !Array.isArray(result.rows)) throw new Error("岗位清单与所选报告不一致。");
      page = targetPage; total = result.total;
      for (const row of result.rows) {
        const details = document.createElement("details"); details.dataset.recordId = row.record_id;
        const kind = row.evidence_level === "full_text" ? "完整正文" : "摘要线索";
        details.append(text("summary", row.title + (row.title_is_excerpt ? "…" : "") + " · " + kind),
          text("p", "来源：" + row.platform + " · " + (row.company || "未记录公司")),
          text("p", "采集时间：" + row.collected_at + " · 岗位记录：" + row.record_id),
          text("p", row.url || "本地导入材料"));
        const body = text("pre", "展开后读取岗位原文"); body.dataset.role = "source-text";
        details.append(body);
        details.addEventListener("toggle", () => loadBody(details, body, row, id, version));
        list.append(details);
      }
      status.textContent = total ? "选择岗位即可展开这份报告中的原文。" : "本报告没有保存岗位原文。";
      pagination.textContent = "第 " + (page+1) + " 页 / " + Math.max(1, Math.ceil(total/20)) + " 页，共 " + total + " 条来源记录";
      navigation.hidden = !total;
    } catch (error) {
      if (current(id, version)) status.textContent = error.message || "原文清单读取失败。";
    } finally {
      if (current(id, version)) { loading = false; buttons(); }
    }
  }
  function setRun(value) {
    const nextRun = /^[a-f0-9]{32}$/.test(value) ? value : "";
    if (nextRun === runId && generation) return;
    runId = nextRun; ++generation; page = 0; total = 0; loading = false;
    root.hidden = !runId; list.replaceChildren(); status.textContent = "";
    pagination.textContent = ""; navigation.hidden = true; buttons();
  }
  setRun("");
  return {setRun};
};
