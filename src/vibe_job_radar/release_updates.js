"use strict";
(() => {
  const $ = id => document.getElementById(id);
  let selected = null, busy = false;
  async function request(path, data) {
    const response = await fetch(path, {method:data ? "POST" : "GET",cache:"no-store",
      headers:{"X-Radar-Token":sessionStorage.getItem("radar-session") || "",...(data ? {"Content-Type":"application/json"} : {})},
      ...(data ? {body:JSON.stringify(data)} : {})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "版本操作未完成。");
    return result;
  }
  function render(state) {
    selected = state.status === "available" ? state : null;
    $("release-current").textContent = "当前版本：" + state.current_version + (state.version ? " / 最新稳定版：" + state.version : "");
    let text = state.message || "";
    if (state.total_bytes) text += " " + Math.floor(100 * state.downloaded_bytes / state.total_bytes) + "%";
    if (state.directory) text += "\n新版目录：" + state.directory;
    $("release-status").textContent = text;
    $("release-check").disabled = busy || state.status === "running";
    $("release-download").disabled = busy || !selected;
    $("release-link").hidden = !state.release_url;
    if (state.release_url) $("release-link").href = state.release_url;
    $("release-notes").textContent = state.notes || "";
    $("release-notes-panel").hidden = !state.notes;
  }
  async function perform(path, data) {
    if (busy) return;
    busy = true; $("release-check").disabled = true; $("release-download").disabled = true;
    try {
      let state = await request(path,data);
      while (state.status === "running") {
        render(state);
        await new Promise(resolve => setTimeout(resolve,700));
        state = await request("/api/releases/state");
      }
      busy = false; render(state);
    } catch (error) {
      busy = false; $("release-status").textContent = error.message;
      $("release-check").disabled = false; $("release-download").disabled = !selected;
    }
  }
  $("release-panel").addEventListener("toggle",() => {
    if ($("release-panel").open && !busy) perform("/api/releases/state");
  });
  $("release-check").addEventListener("click",() => perform("/api/releases/check",{consent:true}));
  $("release-download").addEventListener("click",() => {
    if (selected) perform("/api/releases/download",{consent:true,release_id:selected.release_id,sha256:selected.sha256});
  });
})();
