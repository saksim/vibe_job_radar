"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const panel = $("public-history");
  let pending = false, snapshot = null;
  const kinds = {snapshot:"完整目录更新",not_modified:"确认目录未变化",
    fetch_error:"请求失败",structure_error:"响应结构或内容校验失败"};
  const stamp = value => value === null ? "暂无记录" : new Date(value * 1000).toLocaleString();
  function paragraph(value, className = "") {
    const p = document.createElement("p"); p.textContent = value; p.className = className; return p;
  }
  function render(value) {
    snapshot = value;
    const sections = [];
    for (const source of value.sources || []) {
      const section = document.createElement("section"), title = document.createElement("h3");
      title.textContent = source.label;
      section.append(title);
      const latest = source.history[0];
      section.append(paragraph(latest ? kinds[latest.kind] + " · " + stamp(latest.at) : "尚未记录实际来源请求。"));
      section.append(paragraph("最近成功确认：" + stamp(source.last_confirmed_at) +
        "；正文观测：" + stamp(source.content_observed_at) + "。"));
      if (source.clock_rollback) section.append(paragraph("本机时间早于已保存记录，无法确认新鲜度。", "warning"));
      else if (source.stale) section.append(paragraph("缺少最近10分钟内的成功确认；已有报告可继续查看。", "muted"));
      if (latest?.code) section.append(paragraph("最近错误：" + latest.code + "。历史成功不能替代本次结果。", "warning"));
      const details = document.createElement("details"), summary = document.createElement("summary");
      summary.textContent = "最近 " + source.retained + " 次 / 累计记录 " + source.total_observations + " 次";
      details.append(summary);
      const list = document.createElement("ol");
      for (const event of source.history) {
        const li = document.createElement("li");
        li.append(paragraph(stamp(event.at) + " · " + kinds[event.kind] + (event.code ? " · " + event.code : "")));
        if (event.kind === "snapshot") {
          const c = event.counts;
          li.append(paragraph(event.change_status === "baseline"
            ? "建立基线：" + event.total + " 条，不算新增。"
            : "目录 " + event.total + " 条；新增 " + c.added + "、修改 " + c.updated +
              "、本次未出现 " + c.missing + "、未变 " + c.unchanged + "。"));
          if (event.shape_changed) li.append(paragraph("字段结构摘要发生变化，完整响应已通过适配校验。", "warning"));
        } else if (event.kind === "not_modified") {
          li.append(paragraph("复用 " + event.total + " 条；原正文观测时间：" + stamp(event.observed_at) + "。"));
        }
        list.append(li);
      }
      details.append(list); section.append(details); sections.push(section);
    }
    $("history-sources").replaceChildren(...sections);
    $("history-status").textContent = value.available
      ? "已读取本机历史；记录从本功能启用后开始，未补造旧记录。" + (value.write_warning || "")
      : value.message;
  }
  async function load() {
    if (pending) return;
    pending = true; $("history-refresh").disabled = true; $("history-export").disabled = true;
    try {
      const response = await fetch("/api/public/history", {cache:"no-store",
        headers:{"X-Radar-Token":sessionStorage.getItem("radar-session") || ""}});
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || "无法读取来源历史。");
      render(value);
    } catch (error) {
      snapshot = null; $("history-status").textContent = error.message;
      $("history-sources").replaceChildren();
    } finally {
      pending = false; $("history-refresh").disabled = false;
      $("history-export").disabled = !snapshot?.available;
    }
  }
  panel.addEventListener("toggle", () => { if (panel.open) load(); });
  $("history-refresh").addEventListener("click", load);
  $("history-export").addEventListener("click", () => {
    if (pending || !snapshot?.available) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot,null,2)],{type:"application/json"}));
    const link = document.createElement("a"); link.href = url; link.download = "source-history.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url),1000);
    $("history-status").textContent = "已导出当前显示的本机历史摘要。";
  });
})();
