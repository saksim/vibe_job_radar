**历史记录｜2026-10-06恢复**

> 本页原文来自旧分支归档，保留当时的判断、首次失败和测试记录。原文中的“当前”、检查数量及待办均指原记录时点，不代表现行主干已经通过或仍然缺失。

当前请求排队节奏属于直接CDP的Chrome路径，SDK路径保留自己的实现。原受控复现、原子额度、请求数量/字节/期限边界和未做实站认证的说明均保留。

现行实现与验收边界：[docs/INSTALLED_CHROME_NATIVE.md](INSTALLED_CHROME_NATIVE.md)；[tests/test_native_request_pacing.py](../tests/test_native_request_pacing.py)。

[原始归档文件](https://github.com/saksim/vibe_job_radar/blob/198798667119e354080c444993dc934986c9f17d/docs/NATIVE_REQUEST_PACING.md)；原文SHA256：cc42656d5c6f4675e43dac7663bb145c152c44dfba0e08755b3df2167859536a。本次恢复只补回历史文档，不重新采集或重算旧结果。

---

# Native request pacing

The native controller used to call the synchronous transport wait from inside
`Fetch.requestPaused`. A local reproduction with 20 permitted requests and the
default 0.5-second request interval exhausted a seven-second `Runtime.evaluate`
deadline despite receiving its successful reply. The earlier response-order fix
only excluded events arriving after that reply; preceding callbacks could still
sleep through the deadline.

Request permission, headers, robots and body size are checked before admission.
Chromium keeps a paced request paused. A bounded owner-thread queue retains only
its identifiers and prepared accounting metadata, never its headers or body.
The connection processes queued events before cooperative pacing work. A due
FIFO request retries the same atomic quota reservation and is continued once.
This is a delayed continuation, not an HTTP retry or a reconstructed request.

The existing 128 outstanding-request bound includes queued logical requests;
queued metadata also has a one-megabyte bound. The original short-wait budget
starts at the first reservation attempt for each FIFO request and is not reset
on contention. Hourly/daily limits, publisher windows, cooldown, cancellation,
policy and robots checks remain authoritative. Page/login action budgets are
unchanged. Navigation epochs, browser cancellations and detached sessions retire
queued work. Closing removes the pump callback and clears its private metadata.
New business intent invalidates older observations as soon as it is admitted;
only the eventual continuation consumes a request count and tracks its response.

Deterministic tests cover command responsiveness, actual ledger spacing,
contention, deadlines and refusal/cleanup boundaries. The artificial TLS browser
acceptance also adds 20 permitted stylesheets with the real default interval,
then requires one visible keyword search, the full recorded JD, and the original
report. These tests do not certify a real recruiting-site session or login.


## 取消通知先到、继续命令后被拒绝

2026-10-10 原工作台同任务恢复已越过名称解析阻断，搜索文档返回200，但在队列处理 api-c 请求时停止 native_protocol_error；0职位/JD/报告/登录。旧诊断没有具体协议拒绝原因，因此该实站原因仍未确定。

本机独立 Chrome 的人工回环 HTTP 对照确认：取消后的普通请求可在继续时收到 Invalid InterceptionId；跨域预检的取消表现不同，不能混同。进一步用真实 CDPConnection 与 NativeRequestPacer 的确定性消息顺序重现：取消通知先于拒绝回复进入队列，但在回调内尚未分发，旧程序提前关闭整个任务。

只有 Fetch.continueRequest 返回精确的 -32602 / Invalid InterceptionId，且已有早于回复、同协议会话、同 Network 请求编号的 canceled=true / net::ERR_ABORTED 通知，才清退该已取消请求。通知保持原顺序继续分发；不递归执行回调、不重发、不退回额度、不增加响应或职位成功数。缺少关联通知、其他会话/编号、证书等其他错误和其他协议命令继续停止。

[Fetch 协议](https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/pdl/domains/Fetch.pdl)分别定义拦截编号与可选的 Network 编号；[Network 协议](https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/pdl/domains/Network.pdl)提供请求取消事件。实现保留这种区分，不按域名、URL或文本猜测请求身份。人工协议回归与本地 Chrome 对照不替代猎聘实站因果或正常登录认证。
