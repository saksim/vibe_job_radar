# 猎聘只读搜索与登录边界（PR62 增量）

## 用户需要提供账号吗？

不把登录当作所有读取的前置条件。公开可读的列表/完整正文直接采集；用户明确保存且仍可用的本站 Cookie 按既有机制恢复。只有平台明确要求登录/验证时才进入本人在本机的正常流程。网络错误、未适配资源、robots 不可读、搜索返回零结果都不能自动诊断为“缺少密码”。Cookie存在或列表可读也不证明某个账号已认证。

密码、短信码、二维码会话、Cookie、完整 HAR 和 `.radar-sessions` 不发送到聊天、Issue、模型或公共工件。用户只需在 Radar 自己打开的平台浏览器处理正常登录/必要验证，而不是复制日常浏览器的秘密。

后续增量已实现本机单次授权的正常密码表单填写，见 [密码登录说明](LIEPIN_PASSWORD_LOGIN.md)。实际平台的完整登录、SSO、iframe、服务端会话过期仍需逐站验证。获取账号信息不能弥补尚未适配的网络依赖。

## 本次直接接通的代码路径

在任务开始前明确选择“浏览器原生网络”，沿原浏览器访问搜索页。平台脚本自己产生请求，程序不主动重放 API、不伪造 Cookie/UA/签名：

`www.liepin.com/zhaopin/?key=...` → 限定脚本/样式 → `api-c.liepin.com` 的一个只读 POST及CORS预检 → 私有搜索响应 → LiepinAdapter 卡片 → 原所选详情/Store/同批报告。

只接受路径 `/api/com.liepin.searchfront4c.pc-search-job`，POST及对应OPTIONS；主页面Origin固定，预检方法和请求头检查。不包含投递、消息、账号更新、登录提交或任意API权限。静态资源只接受历史页面出现的`concat.lietou-static.com`限定前缀/文件类型和`image0.lietou-static.com`图片；不将资源域当正文来源。

请求体必须与当前URL关键词、显式筛选和页码一致。默认首个列表页为0，不把后台其他页当当前结果。JSON响应的flag、列表和页码检查；岗位只使用返回的`job.link`，不根据可能不同的`jobId`拼地址；不保存招聘人姓名或联系方式。后发查询优先，不让迟到的旧响应覆盖新响应。响应和请求参数仅在本机内存供配对/解析，不进入诊断/任务持久化。

明确成功且空的jobCardList显示“零条岗位”，不是“请登录”。未知/错误响应不能静默降级为空，也不靠旧列表或历史报告充数。旧DOM与bridge保持，原生不在访问拒绝后自动切换启用。

## 实现依据及证据等级

外部作者于2026-07-23标注的第一手历史请求记录（非官方API承诺，也非当前账号实测）：

- `rockbenben/ai-job-search-cn/.agents/skills/liepin-search/url-reference.md`，Git blob `6cb406899a53be8bf10084d832e3b0c8e2f6ae3b`。描述匿名搜索POST、请求体、响应字段及匿名详情观察。
- 同项目`cli/tests/fixtures/detail-job.html`，blob `1b9306e2f817b6756c00eaf0e3adddca1560a3b0` 的资源引用片段。
- `cli/tests/fixtures/search-response.json`，blob `d9e8e9b3fae448ead8ed6aed276a74a88d1057e8` 的文件元数据，仅作原记录定位，不复制其招聘数据。

这些来源支持实现候选规则；**不证明2026-09-18所有页面免登录或Radar已取得真实JD**。回归文本和测试账户数据独立合成。没有执行外部原始HTML中的脚本或收集其中账号字段。

Playwright网络观察与CDP接口参考官方文档：
https://playwright.dev/python/docs/network
https://chromedevtools.github.io/devtools-protocol/tot/Fetch/

## 本机首次验证与需要协助的信息

第2步选猎聘、一个具体关键词、1页/1岗位，在开始前明确选择原生网络并启用本地脱敏诊断；不需要API Key或密码配置。先观察是否有列表，选1条生成报告。完整正文无AI要求也是有效数据，报告应如实显示零要求。

遇到明确登录/验证时在本机平台窗口完成，正常返回原检索页后可用已有自动接续。不能以拒绝后换身份、出口或后端替代验证。`native_operation_unreviewed`、`resource_domain_blocked`、`robots_unavailable`等依赖问题应保留具体原因，不要求重复输入密码。

需要用户协助的是：**相同关键词在普通浏览器未登录/正常登录后能否看到列表和完整正文；Radar本机一次运行的结果或已预览脱敏诊断。** 只分享关键词、非敏感岗位URL和错误代码/脱敏截图，不分享会话秘密。当前研究运行环境不能代表用户的本机正常账号会话。

## 验收和回滚

`scripts/run_native_liepin_search.py --controlled` 使用三个自有人工HTTPS源，真正的Chromium/Edge原生后端、CORS、API-only无链接页面、实际LiepinAdapter及原报告；它不是实站采集。原四组原生反例/全套UI及单测保持；同一提交结果需单独核验。

现有规则加载、TLS、公网目标检查、共享配额、停止控制保留。主站和业务源的 robots 按[状态和规则](ROBOTS_STATUS_HANDLING.md)分别处理；实际资源/响应变化可能继续阻塞，不能标成 live_verified。不增加后台调度。回滚本增量不删除任何岗位、报告或已保存会话；#49和#50真实目标保持开放。

## 跨域浏览器实现注意

2026-09-23 的真实搜索入口依赖增量及当前未完成项见 [搜索页依赖验证](LIEPIN_PAGE_DEPENDENCIES.md)。官方当前页面还需要筛选项初始化和共享 UI 静态清单；营销/统计请求在本机终止，不再中止主任务。本节以上搜索路径的实站验收仍未完成。

Playwright Chromium 的请求路由会自动合成部分 OPTIONS 成功响应。当前有CORS规则的原生上下文不使用该路由层，直接用CDP控制；未支持目标先取消并安装只阻断的Fetch控制，再由原工作循环关闭，不发送凭据或继续目标。没有CORS的旧契约保留原归属门。必须实测来源收到OPTIONS、来源拒绝时不发POST，以及弹窗首请求未越界；不能仅以函数单测宣称通过。依据：microsoft/playwright `packages/playwright-core/src/server/chromium/crNetworkManager.ts` 的 `isInterceptedOptionsPreflight` 逻辑。
