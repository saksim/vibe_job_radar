# 固定空白页的就绪条件与失败阶段

PR246 在 2026-09-30 的首次 Windows 便携验收失败：源码1874项全0、实际exe的bundled和Chrome原生组件都通过，随后普通离线健康检查返回blank_page/TimeoutError。原run36681236025、工件11081837957及首次24/25结果保留，不重跑该工作流。

最初使用Playwright set_content写入固定HTML，随后明确等待domcontentloaded。PR271首轮的Windows实际exe仍在set_content/6000ms超时：加载事件各1次，无崩溃、浏览器仍连接，不能据此确认SDK内部同步已完成。原run37886154489/job113676501021与工件11596587243保留。

当前检查通过page.goto加载程序内固定的data文档，避开set_content的控制台标记/document.write同步依赖。文档没有脚本或外部资源，CSP为default-src 'none'; base-uri 'none'; form-action 'none'，也不接受用户网址。先等待domcontentloaded，初始6000ms。若超时但已确认唯一主frame提交的是该固定页，当前页未更换、未关闭/崩溃且浏览器仍连接，则继续等待同一页的domcontentloaded；从首次导航开始的就绪总预算15000ms，不重新导航或启动另一浏览器。随后仍精确比较标题；预算耗尽、DOM失败、页面变化、读标题异常或标题不符均失败。原HTTP隔离和实际exe检查保留。

失败阶段保持blank_page_content和blank_page_verify，成功仍为ready/browser_ready。blank_page_check的操作/耗时名为load_inline_page、按需wait_committed_page和read_title；初始超时及继续等待状态分别保留，准确反映实际执行路径。便携报告原固定字段即可保留这个阶段，不需要输出浏览器原始日志、账号或用户路径。启动成功的事实和关闭操作均保持。

回归保留DOM就绪但完整load尚未完成、DOM超时、标题读取异常和错误标题，新增固定离线文档及旧同步信号缺失对照。真实SDK对照仅在测试自有隔离执行环境屏蔽console.debug，验证旧路径等待、新路径完成；注入不代表原CI真的发生同一事件。受控场景只证明检查语义与失败处理，不能证明历史Windows六秒超时的唯一底层原因；后续候选需要自己的首次源码/实际浏览器/实际exe验证，原246失败及相关历史问题不改写为成功。

参考：[Playwright Page.goto](https://playwright.dev/python/docs/api/class-page#page-goto)、[固定版本SDK setContent源码](https://github.com/microsoft/playwright/blob/v1.63.0/packages/playwright-core/src/server/frames.ts)。此项仅本机组件诊断，与网站登录、验证码及实站JD验收分别记录。
## 冻结程序的协议观测

PR313 首次候选在同一个 6000ms 检查超时（run38136564431/job114461529719）。当时主 frame 已提交固定 data 文档，但没有观察到 DOMContentLoaded/load，页面仍存活、浏览器仍连接。该失败保留；它不足以判断是浏览器未产生事件还是 SDK 未送达事件，也不能归因于另一台电脑的 DLP 堆损坏。

Windows 验证器仅为自己启动的第一个测试程序设置 DEBUG=pw:protocol，持续读取其输出，在内存中关联第一次固定检查页导航的 session/frame/loader。报告的 blank_page_protocol 只包含预定义事件、接收相对耗时、与该次 loader 的关系及丢失/读取异常标志；不保存原始日志、URL、协议 ID、标题、账户或请求数据。每行最多 64KiB、事件最多 64 项；超长行整行丢弃，缺失或格式变化如实记出。后续导航不能替代首次观察，接收时间包含管道调度时间。

PR313的协议观测本身不发送额外浏览器命令，不设置PWDEBUG；健康检查当前采用上述有界就绪预算，DOMContentLoaded及标题要求保留。协议记录不把失败变成成功，也不单凭一次后续通过解释历史根因。真实浏览器与 SDK 同一 loader 的事件可帮助下一次失败区分信号在哪一层缺失；程序退出后的 stream_ended 字段用于说明输出是否读完。

## 已提交页面的有界等待

PR321候选29575ec首次24/25，实际exe在原6000ms的load_inline_page失败（run38158818296/job114527275634，工件11708929216）。新增环境/程序绑定已通过；协议记录约529ms收到固定页提交，约7628ms收到执行上下文消息。接收时间含管道调度，不能据此判定底层原因，但原6秒判定没有覆盖这次较晚到达的就绪信号。

因此为已确认提交且存活的同一固定页增加剩余就绪等待，总计最多15秒。未观察提交、异页、崩溃/断连、超出总预算不获得额外等待；不更换浏览器、不访问网站、不把超时本身算为成功。该变更是启动检查的容忍度修复，不宣称已查明#117历史未知根因或本机DLP崩溃。受控时钟测试覆盖迟到成功、8种拒绝条件及继续等待后的超时/页面替换/标题错误，实际新候选结果独立记录。
