# 0.2.0 浏览器采集插件契约

## 分层与复用

`guided/contracts.py`：`SiteAdapter`、`BrowserBackend` Protocol，`Card`、`PageSnapshot` 冻结 dataclass，固定错误代码 `CrawlError`。浏览器并不拥有领域分析逻辑。

`guided/adapters.py`：纯函数式页面语义（站内搜索URL、URL归属与详情识别、原文卡片解析、下一页选择器）。`Registry` 显式注入，重复key拒绝，不因安装了某个包就动态执行其代码。BOSS/猎聘/51job是三个独立实例；默认`not_live_verified`，不是官方API认证。

`guided/browser.py`：可选Playwright后端；浏览器生命周期严格在同一工作线程。只进行正常页面导航、已确认的登录、翻页、取DOM；不调用猜测的私有接口。

`guided/transport.py`：浏览器HTTP由route接管，经过公网DNS检查、IP固定TLS、域名许可再fulfill。没有直接route.continue_，浏览器不会自由跟随HTTP重定向；文档重定向转换为新的受控导航。Service Worker和WebSocket禁用。Cookie只在当前上下文传递，多Set-Cookie分开处理，不写入任务。

`guided/rate.py`：SQLite BEGIN IMMEDIATE原子预约，跨进程/任务共享同一工作区配额，滚动小时/24小时、失败计数不退回、冷却持久化、时钟回拨保护。不同工作区并不是全局同一账号配额，不能以拆工作区规避网站限制。

`guided/service.py`：UI无关任务服务、后台执行、持久化任务、人工接管、选择、停止、报告。工作台不接受账号密码，登录由用户在平台原生页面完成。浏览器会话不跨进程重启保存。

复用原 `JobRecord`（新增source_mode=`browser_fetch`）、`Store`、`parse_job_html`、`analyze` 和证据引擎。个人报告依然保护来源快照和精确要求映射。浏览器任务成功记录先入主库，再以本批选中记录的临时快照生成报告，避免混入历史招聘。

## 显式注册一个站点

下例是**开发者扩展方法，不是普通用户必须填写的JSON**。例子域名仅示意，不是可用招聘服务。

```python
from vibe_job_radar.guided.adapters import DOMAdapter, Registry
from vibe_job_radar.guided.service import GuidedService

adapter = DOMAdapter(
    key="company", label="公司招聘", domains=("careers.example.com",),
    search_base="https://careers.example.com/search", keyword_param="q",
    detail_pattern=r"^/jobs/[a-z0-9-]+$",
    login_url="https://careers.example.com/login",
    login_hosts=("careers.example.com",),
    next_selectors=('a[rel="next"]',),
)
service = GuidedService(workspace, registry=Registry([adapter]))
```

复杂DOM可实现自己的`cards(PageSnapshot)`和`detail(PageSnapshot)`，返回观察到的URL与独立正文；禁止拼造未知职位ID、将整页推荐混入JD。`backend_factory`可注入其他同步后端，实现Protocol以复用任务和分析。

## API 是小接口，不是巨型页面脚本

所有接口继承本机 Host/Origin/会话令牌校验。

| 接口 | 作用 |
|---|---|
| GET /api/guided/state | 平台列表、环境、任务、当前动作与硬限制 |
| POST /api/guided/diagnose | 所选内置平台的DNS检查；不接受任意目标 |
| POST /api/guided/install | 用户确认后执行固定的当前Python安装命令，无shell |
| POST /api/guided/create | 平台+关键词+数量+使用范围，创建站内发现任务 |
| POST /api/guided/action | login / capture / more / search / collect / pause / resume / stop |
| POST /api/guided/export | 从卡片导出已观察/已解析URL，不需要用户整理 |

工作台API不提供自动填密动作，拒绝账号密码字段；任务文件与状态不包含用户名/密码。此版本不持久化Cookie，不支持外部远程控制浏览器或任意脚本执行接口。

## 不兼容与降级

DOM卡片识别基线是实际`a[href]`；没有链接、只通过JS点击事件获取详情的列表需要独立适配，不能以假href代替。密码、扫码、短信等均在所选平台原生页人工完成，本次不提供工作台自动填密。跨站XHR重定向、未允许的写请求、未知资源域名和更严格robots延迟都明确停止。

这是安全约束下的第一版浏览器后端，不是浏览器透明代理的全部兼容实现：相同站点的POST型搜索、SSO域名、复杂iframe等需按真实契约独立接入并测试；不能为了“开箱即用”默认放开所有网络与写操作。

## 验证与发布

运行 `python scripts/run_tests.py` 做离线回归；可选浏览器组件已安装时运行 `python scripts/run_guided_browser.py`。后者以两个真实Chromium进程验证工作台与采集器，远端响应为受控人工站点。网络边界和限频由独立测试覆盖，不允许将fixture传入生产API以关闭安全检查。

新站点必须先纯解析fixture、网络失败/登录/分页测试，再在实际允许环境人工核对结果，记录日期、版本、已观察页数、已执行详情尝试和完整正文数；未知市场分母不报覆盖率。0.2.0新增source_mode，旧程序不保证能读新记录，升级前备份工作区，回退应恢复配套备份。

## 合并前修正的契约

`card_selector` 现在由纯DOM解析真实执行，最终匹配节点必须是带href的锚点。支持标签、`#id`、`.class`、`[attr]`、`[attr="value"]`、后代关系与逗号组，如 `#results .job a[href]`。不支持的CSS（例如伪类、`>`）显式报错，不能回退到整页抓取。复杂选择器请实现自己的`cards`。

自定义Registry无需先修改工作区的全局平台清单；生成报告时把该适配器的平台元数据写入报告配置快照。批次中断仍为已成功记录生成报告，错误状态不变。

`PinnedTransport.fetch(..., required=True)` 默认认为是必要请求；浏览器按document/xhr/fetch与可选资源区分。可选401/403只中断对应请求，429不论资源类型都冷却。重定向、公网校验与TLS校验不放宽。

打开登录按钮使用独立登录动作配额（5分钟/滚动24小时3次），不等同于自动填写密码；native页面内部的请求另计HTTP配额。等待岗位选择时冻结后台请求，明确操作再恢复。

限频后的自动接续还需后端在拥有它的工作线程中提供 `alive() -> bool` 并返回严格的 `True`，且不在认证模式、没有致命错误。暴露 `error` / `wait_error` 的后端必须保持两者一致，普通发布方等待须有对应 `RateLimit`。没有该可选能力、窗口关闭或状态检查出错时转为明确操作，不自动创建新浏览器；原定时动作绑定原后端对象，排队后失效也不能借用其他窗口。旧插件仍可通过用户操作执行原 Protocol，只有自动定时续接需要上述生命期证明。详见 [等待中的窗口关闭](DEFERRED_BROWSER_LIFECYCLE.md)。
