# 固定空白页的就绪条件与失败阶段

PR246 在 2026-09-30 的首次 Windows 便携验收失败：源码1874项全0、实际exe的bundled和Chrome原生组件都通过，随后普通离线健康检查返回blank_page/TimeoutError。原run36681236025、工件11081837957及首次24/25结果保留，不重跑该工作流。

最初使用Playwright set_content写入固定HTML，随后明确等待domcontentloaded。PR271首轮的Windows实际exe仍在set_content/6000ms超时：加载事件各1次，无崩溃、浏览器仍连接，不能据此确认SDK内部同步已完成。原run37886154489/job113676501021与工件11596587243保留。

当前检查通过page.goto加载程序内固定的data文档，避开set_content的控制台标记/document.write同步依赖。文档没有脚本或外部资源，CSP为default-src 'none'; base-uri 'none'; form-action 'none'，也不接受用户网址。继续等待domcontentloaded、使用原6000ms限时并精确比较标题；DOM超时、读标题异常或标题不符仍失败，不重试、不关闭HTTP隔离、不绕过最终实际exe资格。

失败阶段保持blank_page_content和blank_page_verify，成功仍为ready/browser_ready。blank_page_check的操作/耗时名为load_inline_page和read_title，准确反映实际执行路径。便携报告原固定字段即可保留这个阶段，不需要输出浏览器原始日志、账号或用户路径。启动成功的事实和关闭操作均保持。

回归保留DOM就绪但完整load尚未完成、DOM超时、标题读取异常和错误标题，新增固定离线文档及旧同步信号缺失对照。真实SDK对照仅在测试自有隔离执行环境屏蔽console.debug，验证旧路径等待、新路径完成；注入不代表原CI真的发生同一事件。受控场景只证明检查语义与失败处理，不能证明历史Windows六秒超时的唯一底层原因；后续候选需要自己的首次源码/实际浏览器/实际exe验证，原246失败及相关历史问题不改写为成功。

参考：[Playwright Page.goto](https://playwright.dev/python/docs/api/class-page#page-goto)、[固定版本SDK setContent源码](https://github.com/microsoft/playwright/blob/v1.63.0/packages/playwright-core/src/server/frames.ts)。此项仅本机组件诊断，与网站登录、验证码及实站JD验收分别记录。
