# 固定空白页的就绪条件与失败阶段

PR246 在 2026-09-30 的首次 Windows 便携验收失败：源码1874项全0、实际exe的bundled和Chrome原生组件都通过，随后普通离线健康检查返回blank_page/TimeoutError。原run36681236025、工件11081837957及首次24/25结果保留，不重跑该工作流。

源码使用Playwright set_content写入固定且没有资源的HTML。官方API和本机1.63 SDK确认默认等待load；检查只需要DOM已解析并能读出正确标题。现在明确等待domcontentloaded，继续使用原6000ms限时与原标题比较；DOM超时、读标题异常或标题不符仍失败，不重试、不关闭HTTP隔离、不绕过最终实际exe资格。

失败阶段细分为blank_page_content和blank_page_verify，成功仍为ready/browser_ready。便携报告原固定字段即可保留这个阶段，不需要输出浏览器原始日志、账号或用户路径。启动成功的事实和关闭操作均保持。

新回归覆盖DOM就绪但完整load尚未完成、DOM超时、标题读取异常和错误标题。受控场景只证明检查语义与失败处理，不能证明历史Windows六秒超时的唯一底层原因；后续候选需要自己的首次源码/实际浏览器/实际exe验证，原246失败及相关历史问题不改写为成功。

参考：[Playwright Page.set_content](https://playwright.dev/python/docs/api/class-page#page-set-content)。此项仅本机组件诊断，与网站登录、验证码及实站JD验收分别记录。
