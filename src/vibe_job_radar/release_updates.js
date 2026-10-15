"use strict";
(() => {
  const $ = id => document.getElementById(id);
  let selected = null, installSelected = null, busy = false, installing = false;
  async function request(path, data) {
    const response = await fetch(path, {method:data ? "POST" : "GET",cache:"no-store",
      headers:{"X-Radar-Token":sessionStorage.getItem("radar-session") || "",...(data ? {"Content-Type":"application/json"} : {})},
      ...(data ? {body:JSON.stringify(data)} : {})});
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.error || "版本操作未完成。");
      error.responseReceived = true; throw error;
    }
    return result;
  }
  function render(state) {
    selected = state.status === "available" ? state : null;
    const next = ["available","ready"].includes(state.status) && state.automatic_supported ? state : null;
    if (next?.release_id !== installSelected?.release_id || next?.sha256 !== installSelected?.sha256)
      $("release-install-consent").checked = false;
    installSelected = next;
    $("release-auto-controls").hidden = !state.automatic_supported;
    $("release-install").disabled = busy || !installSelected || !$("release-install-consent").checked;
    $("release-install-consent").disabled = busy || !installSelected;
    $("release-current").textContent = "当前版本：" + state.current_version + (state.version ? " / 最新稳定版：" + state.version : "");
    let text = state.message || "";
    if (state.total_bytes) text += " " + Math.floor(100 * state.downloaded_bytes / state.total_bytes) + "%";
    if (state.directory) text += "\n新版目录：" + state.directory;
    if (state.last_install) {
      text += "\n" + state.last_install.message;
      if (state.last_install.backup) text += "\n备份目录：" + state.last_install.backup;
    }
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
    busy = true; installing = path === "/api/releases/install";
    $("release-check").disabled = true; $("release-download").disabled = true;
    $("release-install").disabled = true;
    try {
      let state = await request(path,data);
      while (state.status === "running") {
        render(state);
        await new Promise(resolve => setTimeout(resolve,700));
        state = await request("/api/releases/state");
      }
      busy = state.status === "restarting"; render(state);
    } catch (error) {
      busy = false; $("release-status").textContent = installing && !error.responseReceived
        ? "升级连接已结束。请等待新版窗口；若未打开，请核对工作区 updates 中的升级记录，原程序和数据保留。"
        : error.message;
      $("release-check").disabled = false; $("release-download").disabled = !selected;
      $("release-install-consent").disabled = !installSelected;
      $("release-install-consent").checked = false;
    }
  }
  $("release-install-consent").addEventListener("change", () => {
    $("release-install").disabled = busy || !installSelected || !$("release-install-consent").checked;
  });
  $("release-install").addEventListener("click", () => {
    if (installSelected && $("release-install-consent").checked) perform("/api/releases/install", {
      consent:true, consent_version:installSelected.install_consent_version,
      release_id:installSelected.release_id, sha256:installSelected.sha256
    });
  });
  $("release-panel").addEventListener("toggle",() => {
    if ($("release-panel").open && !busy) perform("/api/releases/state");
  });
  $("release-check").addEventListener("click",() => perform("/api/releases/check",{consent:true}));
  $("release-download").addEventListener("click",() => {
    if (selected) perform("/api/releases/download",{consent:true,release_id:selected.release_id,sha256:selected.sha256});
  });
})();
