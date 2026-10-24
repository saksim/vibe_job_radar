/* Loaded only by the authenticated server adapter, before existing page scripts. */
(() => {
  const panel = document.createElement("section");
  panel.id = "server-runtime";
  const title = document.createElement("strong");
  title.textContent = "服务器私人工作区";
  const scope = document.createElement("p");
  scope.textContent = "数据和任务保存在服务器。此处的网络请求、目录和“本机”均指服务器；下载的文件才保存到当前设备。桌面浏览器采集、开机启动、程序内升级和网络配置请使用对应的桌面入口或由部署者管理。";
  const logout = document.createElement("button");
  logout.type = "button"; logout.textContent = "退出登录";
  logout.addEventListener("click", async () => {
    logout.disabled = true;
    try {
      const response = await fetch("/api/session/logout", {method:"POST", cache:"no-store",
        headers:{"Content-Type":"application/json", "X-Radar-Token":sessionStorage.getItem("radar-session") || ""}, body:"{}"});
      if (!response.ok && response.status !== 401) throw new Error("退出未完成，请重试。");
      sessionStorage.removeItem("radar-session"); location.replace("/login");
    } catch (error) {scope.textContent = error.message; logout.disabled = false;}
  });
  panel.append(title, scope, logout);
  document.querySelector("main")?.prepend(panel);
  for (const id of ["research-goal", "network-preferences", "release-panel", "windows-startup", "team-protect", "team-credential-disclosure"]) {
    const node = document.getElementById(id);
    if (node) node.hidden = true;
  }
  const research = document.getElementById("research-goal");
  if (research?.nextElementSibling) research.nextElementSibling.hidden = true;
  const introduction = document.querySelector("#research-start > p");
  if (introduction) introduction.textContent = "选择下方公开来源或上传已有 JD，再查看本批要求与个人证据。需要招聘平台登录时使用桌面版。";
  for (const link of document.querySelectorAll('a[href^="/guided"]')) link.hidden = true;
})();
