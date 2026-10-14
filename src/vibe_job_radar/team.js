"use strict";
(() => {
  const $ = id => document.getElementById(id);
  let busy = false, configured = false;
  async function request(path, body) {
    const response = await fetch(path, {method:body ? "POST" : "GET", cache:"no-store",
      headers:{"X-Radar-Token":sessionStorage.getItem("radar-session") || "", ...(body ? {"Content-Type":"application/json"} : {})},
      ...(body ? {body:JSON.stringify(body)} : {})});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || "团队服务请求失败。");
    return value;
  }
  async function perform(action) {
    if (busy) return;
    busy = true; $("team-check").disabled = true; $("team-sync").disabled = true; $("team-reset").disabled = true;
    try { await action(); }
    catch (error) { $("team-status").textContent = error.message; }
    finally { busy = false; $("team-check").disabled = !configured; $("team-sync").disabled = !configured; $("team-reset").disabled = false; }
  }
  $("team-panel").addEventListener("toggle", () => { if ($("team-panel").open) perform(async () => {
    const state = await request("/api/team/state"); configured = state.configured && !state.error;
    $("team-status").textContent = state.message + (state.origin ? " 服务：" + state.origin : "");
  }); });
  $("team-check").addEventListener("click", () => perform(async () => {
    const state = await request("/api/team/check", {consent:true});
    $("team-result").textContent = "成员：" + state.member + " / 设备：" + state.device + " / 角色：" + state.role + "\n获准来源：" + state.sources.join("、") + "\n配额资源：" + state.resources.map(r => r.site + " / " + r.id + "（今日页面 " + r.usage.page.day + "/" + r.limits.pages_day + "，请求 " + r.usage.request.day + "/" + r.limits.requests_day + "）").join("、") + "\n已确认水位：" + state.watermarks.map(w => w.revision).join("、");
    $("team-result").hidden = false; $("team-status").textContent = "已连接并核对当前设备权限。";
  }));
  $("team-sync").addEventListener("click", () => perform(async () => {
    $("team-status").textContent = "正在同步已批准的公开目录，请等待完成。";
    let state = await request("/api/team/sync", {consent:true,reset:$("team-reset").checked});
    while (state.status === "running") {
      await new Promise(resolve => setTimeout(resolve, 800));
      state = (await request("/api/team/state")).sync;
    }
    if (state.status !== "completed") throw new Error(state.message + (state.code ? " " + state.code : ""));
    $("team-result").textContent = "版本：" + state.revision + " / 当前目录：" + state.jobs + " 条 / 本次事件：" + state.events + " 条 / 服务端确认：" + (state.acknowledged ? "已确认" : "待确认");
    $("team-result").hidden = false; $("team-status").textContent = state.message;
  }));
})();
