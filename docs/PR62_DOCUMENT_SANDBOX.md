# 原生跨域采集：先限制辅助窗口，再执行网页

关联 #49 / #50 / #46，仍在 PR62 内；不需要用户提前陪测或提供登录资料。

## 真实失败而非推测

读取到中断前新增提交 `bfec5fe45a4db47c2d8ea79bf5fd632ff8280c4a` 的 Windows headed 原生工件：匿名 API 搜索、完整 JD 到报告、自动选择采集和明确零结果均通过。但随后的弹窗反例记录了 `/apply` 的首个 HTTP GET，且没有应用标识，最终检查失败。这里的 `/apply` 是人工服务器的无副作用测试路径，绝非真实投递。

不能以先前某轮绿色宣称拒绝弹窗总能及时生效。公开 page/target 事件与临时 Fetch 隔离存在时序竞争，必须在网页执行之前限制其创建辅助窗口。

## 修正及明确的架构取舍

只对启用了已声明 CORS 业务契约的原生上下文，在既有响应检查通过后，为 Document 响应追加一条独立的 enforcing CSP：

`sandbox allow-scripts allow-same-origin allow-forms`

由浏览器在文档执行前实施，不授权 popups/popups-to-escape-sandbox、下载或其他额外能力。脚本、原站点 origin、Cookie 和同窗口的正常表单仍可运行，所有实际请求仍须经过原角色/域名/方法/robots/配额/取消检查。没有伪造接口、自动填写密码或放开跨源 SSO。

这是对既有“完全不改文档响应”保证的一处明确收窄：浏览器先完成获准请求和TLS验证，CORS上下文的Document响应在原状态/大小/权限检查后读取原生返回的正文，重新以**同一份解码字节**交付并追加更严格CSP，不重复任何HTTP请求。原CSP/Set-Cookie等重复头保留；Content-Encoding、Transfer-Encoding和旧Content-Length不用于已解码的本地表示，长度重新计算。其他类型的预检/业务/资源响应完全原样，源站拒绝仍阻止POST。不可宣称所有响应头/响应元数据逐字未变或仍具有完整流式文档呈现。

最多接受5,000,000个解码正文bytes；读取后、交付前再次检查同一目标未退休、取消、策略和原停止状态，过大或格式异常停止，不重试/降级。GetResponseBody由浏览器缓冲并返回数据后做本地长度检查，因此该上限不是对浏览器内部缓冲内存的硬隔离保证。零正文204/205保持原生。真实证书失败在读取前就由原浏览器/状态处理停止，不用本地交付制造成功。

初版2d4仅使用Fetch.continueResponse追加头：Linux headed仍出现真正的辅助page而停止，未再记录/apply。被动事件记录确认不是浏览器地址栏误报。Chromium第一方源码的头部覆盖分支只更新URLResponseHead.headers并继续转交原head，未重建已解析策略；这是头部修改不可靠的一个有源码依据的解释，而非所有版本的断言。本次改用原文档字节的显式交付，使浏览器对完整响应重新处理策略，不忽略该失败。

不使用 JavaScript 重写 window.open，不依靠延迟关闭，不通过 Playwright 全局路由合成 OPTIONS，不启用 bypass_csp 或 ignore_https_errors。非 CORS 原生上下文及旧 bridge 不受此次文档策略影响。协议调用失败继续停止，绝不降级为不加限制后继续。

**兼容边界：** 需要新窗口的登录或业务在该模式仍不支持。此前这些目标就不在支持范围；不能为了通过测试将其宣称已接通。使用同一受控页面的正常登录衔接继续保留。隐藏登录模板误判、真实平台表单/SSO 适配和第一条真实 JD 连续验收仍未完成，本变更不重交此前被阻断的登录判断修正。

## 开发验证

单元测试覆盖新增限制与原 CSP 并存、重复 Set-Cookie 和压缩头保留、真实 CORS 响应不变、robots/错误文档、拒绝响应及协议失败不降级。

四组生产原生浏览器保留原全部正常和反例路径，增加页面最初内联脚本、普通/无 opener 的 window.open、target=_blank 链接和 POST 表单的多次尝试；服务器必须零收到 /apply，主岗位页和已取得搜索数据仍可用。人工源自己明确允许 popups，证明是额外限制生效；原有正常 CORS 预检/POST、自动查询到报告、来源拒绝不发 POST 保持。

这些是人工源测试，不等于实际猎聘、用户账号、当前网络或所有第三方站点认证。当前 PR 按已知功能问题和实际验证决定是否可合并，不只看测试数量。

## 公开依据

- W3C CSP3 sandbox 与多策略并行：https://www.w3.org/TR/CSP3/#directive-sandbox ，https://www.w3.org/TR/CSP3/#multiple-policies
- Chrome DevTools Protocol Fetch.getResponseBody/fulfillRequest：https://chromedevtools.github.io/devtools-protocol/tot/Fetch/
- Chromium请求拦截器第一方源码（header-only与ProcessResponseOverride分支）：https://chromium.googlesource.com/chromium/src/+/refs/heads/main/content/browser/devtools/devtools_url_loader_interceptor.cc
- Playwright 新 page 事件可能在首次请求响应开始后才触发：https://playwright.dev/python/docs/api/class-browsercontext#browser-context-event-page
