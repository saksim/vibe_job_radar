"use strict";
// Observes one application-owned task. This component never advances report work.
window.createReportTasks = function ({request, status, open, refresh, accepts, onComplete,
                                      onAccepted = () => {}, onBusy = () => {}, shouldOpen = () => true}) {
  let saved = null, pending = false, reading = false, opening = false, generation = 0, timer = null;
  let following = "", firstObservation = true;
  let suspended = false, restoring = false, starting = false, viewVersion = 0;
  const busy = () => Boolean(saved?.busy || pending || starting || restoring);
  function notifyBusy() { onBusy(busy()); }
  const shown = new Set();
  const validId = value => typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
  function controls() {
    refresh.disabled = pending || reading;
    open.disabled = pending || opening || restoring;
    open.hidden = !saved?.task || saved.busy || saved.task.status !== "succeeded" || !validId(saved.task.report_id);
  }
  function schedule() {
    clearTimeout(timer);
    if (!suspended && saved?.busy) timer = setTimeout(() => { void observe(); }, 1000);
  }
  async function show(task, explicit) {
    if (opening || !validId(task.report_id)) return;
    if (!accepts(task)) {
      if (explicit) location.assign('/#report=' + task.report_id);
      return;
    }
    if (!explicit && (shown.has(task.id) || task.id !== following || !shouldOpen(task))) return;
    const viewed = viewVersion;
    const current = () => !suspended && viewed === viewVersion && task.id === saved?.task?.id &&
      (explicit || (task.id === following && shouldOpen(task)));
    opening = viewed + 1; controls();
    try {
      const displayed = await onComplete(task, explicit, current);
      if (displayed !== false && viewed === viewVersion && task.id === saved?.task?.id) shown.add(task.id);
    } finally { if (opening === viewed + 1) { opening = false; controls(); } }
  }
  async function receive(value) {
    if (!value || typeof value.busy !== "boolean" || (value.task &&
        (!validId(value.task.id) || !["running", "succeeded", "failed", "interrupted"].includes(value.task.status))))
      throw new Error("报告状态不可识别；未重新提交任务，请刷新状态核对。");
    saved = value; restoring = false;
    notifyBusy();
    const task = value.task;
    if (firstObservation) {
      firstObservation = false;
      if (task && accepts(task) && shouldOpen(task)) following = task.id;
    }
    if (!task) status.textContent = "尚无后台报告任务。生成前请核对已保存的输入。";
    else {
      const label = task.kind === "evidence" ? "个人报告" : "分析报告";
      const prefix = `${label}任务 ${task.id.slice(0,8)}：`;
      if (value.busy) status.textContent = prefix + (value.owned_elsewhere ? "由另一个工作台进程处理。" : "正在后台生成。") + "关闭网页不会终止本次任务，请勿重复提交。";
      else if (task.status === "succeeded") status.textContent = prefix + "已完成，原报告继续保留。";
      else if (task.status === "interrupted") status.textContent = prefix + "上次运行已中断，未自动重放。请先核对历史报告，再决定是否重新生成。";
      else if (task.status === "failed") status.textContent = prefix + (task.error?.message || "生成失败，请检查输入和工作区；没有自动重试。");
      else status.textContent = prefix + "执行状态待核对；请刷新状态，勿重复提交。";
    }
    controls(); schedule();
    if (task?.status === "succeeded" && !value.busy) await show(task, false);
  }
  async function observe() {
    if (suspended || pending || reading) return;
    const observed = ++generation;
    reading = true; controls();
    try {
      const value = await request('/api/reports/state');
      if (observed === generation && !pending) await receive(value);
    } catch (error) {
      if (observed === generation && !pending) {
        clearTimeout(timer);
        status.textContent = (error.message || "暂时无法读取报告状态。") + " 页面没有重新提交任务；可点击刷新报告状态。";
      }
    } finally {
      if (observed === generation) { reading = false; controls(); }
    }
  }
  async function start(data) {
    if (suspended || busy()) throw new Error("已有报告任务正在执行，请等待后再生成。");
    const submitted = ++generation; viewVersion++; opening = false;
    reading = false; pending = true; starting = true; clearTimeout(timer);
    notifyBusy(); controls();
    try {
      const value = await request('/api/reports/start', data);
      if (submitted !== generation || suspended) return;
      if (!value?.task || !validId(value.task.id)) throw new Error("未收到有效任务编号，请刷新状态核对；不要重复提交。");
      following = value.task.id; firstObservation = false;
      onAccepted(value.task);
      await receive(value);
    } catch (error) {
      if (submitted !== generation || suspended) return;
      restoring = true;
      status.textContent = (error.message || "任务提交结果未知。") + " 请先刷新报告状态；页面不会自动再次提交。";
      throw error;
    } finally {
      starting = false;
      if (submitted === generation) pending = false;
      else if (!suspended) restoring = true;
      notifyBusy(); controls(); schedule();
      // A receipt arriving after page restoration cannot select a report. Read
      // current ownership again before enabling another submission instead.
      if (submitted !== generation && !suspended) void observe();
    }
  }
  refresh.addEventListener('click', () => { void observe(); });
  open.addEventListener('click', async () => {
    const task = saved?.task;
    if (saved?.busy || task?.status !== 'succeeded') return;
    try { await show(task, true); }
    catch (error) { status.textContent = error.message || "无法打开报告，请核对历史记录。"; }
  });
  window.addEventListener('pagehide', () => {
    suspended = true; restoring = true; clearTimeout(timer); generation++; viewVersion++;
    reading = false; pending = false; opening = false;
    if (starting) firstObservation = true;
    notifyBusy(); controls();
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    suspended = false; restoring = true;
    notifyBusy(); controls();
    void observe();
  });
  controls();
  return {start, observe};
};
