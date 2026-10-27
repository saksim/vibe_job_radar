# 开发与实现契约

[返回文档目录](../README.md)

以下文档解释当前代码的边界及具体修复背景。文中的首次测试和旧PR结果只对应所列提交；当前发行状态见[STATUS](../STATUS.md)，冻结的旧候选另在[历史归档](../archive/README.md)。

## 架构、插件与用例边界

- [当前架构：分层模块化单体](../ARCHITECTURE.md)
- [0.2.0 浏览器采集插件契约](../BROWSER_PLUGIN_CONTRACT.md)
- [D01：采集诊断与原生浏览器访问契约](../ACQUISITION_NATIVE_ADR.md)

## 规则与引用边界

- [明确使用 AI 的长句要求](../EXPLICIT_AI_APPLICATION.md)
- [明确使用语言模型审查代码](../MODEL_CODE_REVIEW.md)
- [英文正文的岗位要求与提及边界](../ENGLISH_OBLIGATION_BOUNDARIES.md)
- [编号中文职责的续行与原文证据](../NUMBERED_ITEM_WRAPS.md)

## 存储、所有权与持久化

- [登录返回后等待完整报告的功能验收（2026-10-09）](../DURABLE_REPORT_WRITES.md)
- [已初始化职位数据库的报告读取](../STORE_READ_OPEN.md)
- [已有配额库打开不再申请初始化写锁](../RATE_LEDGER_OPEN.md)
- [Windows 本机 HTTP 拒绝响应（#65）](../LOCAL_HTTP_REFUSALS.md)

## 开发、构建与失败证据

- [开发与发版晋升](../BRANCH_PROMOTION.md)
- [当前主干的源码绑定验收与候选打包](../SOURCE_QUALIFICATION.md)
- [整套回归预算快照](../SUITE_BUDGET_SNAPSHOT.md)
- [从合格 Windows 候选准备和发布版本](../PORTABLE_RELEASE.md)
- [Windows 发行者签名与 unsigned](../PUBLISHER_SIGNING.md)
- [自动续采等回归失败的代码位置](../TEST_FAILURE_LOCATION.md)
- [回归测试失败瞬间证据](../TEST_FAILURE_EVIDENCE.md)
- [Current browser and source qualification budgets](../BROWSER_SOURCE_JOB_BUDGETS.md)
- [会话验收等待报告提交](../REPORT_COMPLETION_BROWSER_CHECK.md)

## 采集执行与恢复契约

- [有限主文档重试](../BOUNDED_READ_RETRY.md)
- [等待期间关闭采集窗口（#73）](../DEFERRED_BROWSER_LIFECYCLE.md)
- [列表结束时的持久化边界](../GATHER_CHECKPOINTS.md)
- [向导操作与状态轮询交接](../GUIDED_ACTION_HANDOFF.md)
- [浏览器进度只更新当前动作](../GUIDED_PROGRESS_CHECKPOINT.md)
- [向导首次操作就绪（#77）](../GUIDED_STARTUP.md)
- [浏览器采集：同工作区只由一个服务控制会话](../GUIDED_TASK_OWNERSHIP.md)
- [混合模式核心施工：第一批运行时交付](../HYBRID_CORE_PHASE1.md)
- [HTTP失败任务转交浏览器：不用重新填写原条件](../HYBRID_HTTP_HANDOFF.md)
- [混合模式第一批：发布方限频与可恢复等待](../HYBRID_PHASE1_PACING.md)
- [Public lifecycle wait evidence](../PUBLIC_LIFECYCLE_WAIT_DIAGNOSTICS.md)
- [Windows public-owner wait evidence](../PUBLIC_OWNER_WAIT_DIAGNOSTICS.md)
- [公开查询与旧报告响应的交接](../PUBLIC_POLL_HANDOFF.md)
- [公开任务：停止与确认继续](../PUBLIC_TASK_LIFECYCLE.md)
- [同工作区公开任务所有权](../PUBLIC_TASK_OWNERSHIP.md)

## 站点、登录与原生访问契约

- [CDP回复与延后事件顺序](../CDP_RESPONSE_ORDER.md)
- [猎聘批次与恢复进展（#51 / #52）](../LIEPIN_BATCH_RECOVERY.md)
- [D03：已返回页面的岗位身份与正文提取](../LIEPIN_DETAIL_D03.md)
- [可选热门词不应中断正常搜索](../LIEPIN_HOTWORDS.md)
- [猎聘登录界面配置读取](../LIEPIN_LOGIN_UI_CONFIG.md)
- [猎聘正常搜索的分页控件（#51）](../LIEPIN_NORMAL_PAGINATION.md)
- [猎聘正常搜索表单](../LIEPIN_NORMAL_SEARCH_FORM.md)
- [猎聘当前搜索页依赖验证（2026-09-23，PR62）](../LIEPIN_PAGE_DEPENDENCIES.md)
- [猎聘公开详情：复制链接到本批报告](../LIEPIN_PUBLIC_DETAILS.md)
- [猎聘正常表单的请求配对](../LIEPIN_QUERY_BINDING.md)
- [猎聘已返回正文解析修复：裸换行 JSON-LD 与无 h1 页面](../LIEPIN_RECORDED_LAYOUT.md)
- [猎聘城市目录依赖](../LIEPIN_REGION_DEPENDENCY.md)
- [猎聘页面初始化与关键词结果](../LIEPIN_SEARCH_INITIALIZATION.md)
- [猎聘只读搜索与登录边界（PR62 增量）](../LIEPIN_SEARCH_NATIVE.md)
- [猎聘当前安全组件的固定配置读取](../LIEPIN_SECURITY_CONFIG.md)
- [正常登录跳转后的稳定观察](../LOGIN_RETURN_NAVIGATION.md)
- [从登录入口继续原关键词](../LOGIN_SEARCH_ENTRY.md)
- [D02：原生浏览器实验后端](../NATIVE_BROWSER_D02.md)
- [暂停招聘详情保留原状态](../NATIVE_CLOSED_DETAIL.md)
- [原生控制层：减少页面运行时事件采集](../NATIVE_EVENT_COLLECTION.md)
- [本地原生浏览器代理的连接接收容量](../NATIVE_LISTENER_CAPACITY.md)
- [从正常登录继续原岗位任务](../NATIVE_LOGIN_CONTINUATION.md)
- [原生认证代理验收的失败现场](../NATIVE_PROXY_AUTH_EVIDENCE.md)
- [Refused requests when the page closes](../NATIVE_REJECTED_ROUTE_CLOSE.md)
- [Native request pacing](../NATIVE_REQUEST_PACING.md)
- [原生搜索请求与本次结果绑定](../NATIVE_SEARCH_QUERY_BINDING.md)
- [猎聘搜索和登录页面的读取依赖](../NATIVE_SEARCH_READ_DEPENDENCIES.md)
- [猎聘原生可见搜索与登录返回](../NATIVE_VISIBLE_SEARCH_FORM.md)
- [原生 CI 等待诊断（#111 / #112）](../NATIVE_WAIT_EVIDENCE.md)
- [登录后直接接回所选完整 JD](../SELECTED_DETAIL_LOGIN_RETURN.md)
- [首站页面加载：先修真实 HTTP 请求，再验证真实岗位](../SITE_LOADING_ITERATION.md)

## 机器证据与许可原文

- [PR220 固定交接证据](../evidence/pr220-handoff.json)
- [已标注公开数据复核证据](../evidence/labelled-public-recheck.json)
- [QuickJS-NG 许可声明](../licenses/QUICKJS_NG_NOTICES.txt)
