# D02：原生浏览器实验后端

专项 #46，步骤 #48；前置 D01 / PR58 已合并。
开发基线 `main@03c2491996f4b506e9c29434d32bb34cff6d26aa`。
本文件描述本次源码候选；合并、受控浏览器通过、用户现场可用是不同状态。

当前#63候选将native的页面控制改为[按需DOM/Input与最少事件采集](NATIVE_EVENT_COLLECTION.md)，保留以下站点网络控制。该候选用新建应用进程的继承管道通信，不开放调试端口或连接已有日常浏览器；默认bridge仍使用Playwright。真实搜索与账号验收仍未完成。

## 改变了什么

旧 `bridge` 仍为默认。创建新任务时显式选择 `native`，阅读说明并确认后，
才使用应用专有的新浏览器实例。已有任务不能变更后端，失败不自动切换路线。

```text
原生 Chromium/Edge 页面与 Fetch Request/Response 控制
  → 应用本机、会话认证的 CONNECT 隧道（仅透明传输加密字节）
  → 已选择的本机 HTTP/SOCKS 路线或系统直连
  → 校验过的公网目标 IP
  → 浏览器自己的 TLS/SNI/Cookie/压缩/响应和渲染
```

这不是把原 `PinnedTransport.fetch` 换个名字。原生模式的 HTTP 由浏览器发送，
Python 不建立目标 TLS、不重建 Cookie、不解压页面、不伪造 302，不 `route.fulfill`。
`NativeControl.fetch` 明确禁止调用。Python 仅在本机 CONNECT 建连前复用现有
DNS/公网/网络偏好验证，并透明传输加密字节。没有安装产品 CA 或关闭证书验证。

浏览器被明确配置为经过此本机隧道，因此不承诺继承日常浏览器扩展/PAC。
兼容范围仍限于已实现的匿名本机 HTTP/SOCKS5、现有网络偏好及明确同意的
198.18/15 加密解析；不修改系统代理，不扫描局域网，不要求关闭正常 VPN/TUN。
选定的上游代理失败不自动直连。本分支在上述基线上增加[显式本机代理认证](LOCAL_PROXY_AUTH.md)；上游凭据与浏览器访问应用自有隧道的临时凭据隔离，拒绝或握手结果不明时不由同一原生会话重复认证。

## 逐请求控制与数据观察

使用 Chromium CDP 的 Fetch 请求/响应阶段；不用 Playwright HTTP Route 重发或
回填数据。原因是本次检查的 Playwright Chromium 实现会跳过重定向后的用户
route 回调，也可能在拦截模式合成预检响应。D02 必须逐跳计量，并让预检自然执行。
协议失败关闭应用自有页面，不把不完整拦截当成功。

站点契约是受审查的 Python 数据类，不接受网页提交任意域名、正则或 endpoint。
精确主机、方法、资源类型、路径和业务用途匹配后才允许。未知 GET 也不自动视为
只读；已核实的搜索 POST 与明确登录阶段的 POST 分开；投递、发消息等未声明操作
不能通过“POST 全开”执行。资源依赖不得由页面自我授权。

同源跳转由浏览器处理，但每跳重新检查规则和配额，最多5跳；跨源跳转目前停止，
不把凭据跨域问题当成天然解决。只在操作前建立观察，任务epoch隔离旧响应。
业务 JSON 只在本地内存供后续专用适配器读取，最多20份、单份1MB、合计4MB。
它不是公共诊断的一部分。诊断仍只导出脱敏元数据，不导出完整 HAR/Cookie/正文。

窗口、命令、请求、响应缓冲均有限。SW/WebSocket/Worker/OOPIF等当前未完整覆盖
的执行面阻断；同页面子frame也不视为已支持。浏览器HTTP缓存关闭。不能把这些
兼容限制包装成“全部现代招聘页面支持”。取消后的新请求停止；已经发出的请求
不能撤回，CONNECT关闭会中断仍在途的字节传输。

## 访问规则和频率

robots由同一浏览器、同一路线获取，当前只接受有规则的200 UTF-8文本。
HTML/空响应/重定向/非200保持不可用，不以未知状态推断允许。新后端实现独立的
有界匹配，支持通配、末尾锚点、最长匹配、Allow同长度优先及产品组选择；旧后端
parser不修改。发布方的更严格间隔/窗口合并到原有持久RateLedger。
robots不是业务许可；页面能够打开也不自动授权所有操作或对外分发。

浏览器文档、业务、资源、规则和登录请求分别计数，并使用既有站点/工作区共享额度；
登录仍遵守已有窗口。切换任务、后端或重启不能清掉配额或429冷却。
401/403、验证页、未知业务或协议不完整停止；不轮换UA、账号或IP。

公网目标绑定在每个CONNECT连接建立时执行，而不是每一个HTTP请求重查DNS。
连接复用仍指向已验证的同一公网IP；请求级规则/配额由Fetch执行。该保证与旧Python
逐请求建立连接不完全相同。没有宣称对浏览器实现缺陷、内核漏洞或所有后台网络提供
完整系统沙箱；对强隔离部署仍需要额外系统出站控制，本地实验不擅自调整系统策略。

## 当前平台状态：不是三站已经打通

当前仅提供猎聘**主站入口/同主机静态资源的bootstrap契约**，没有凭空填入搜索API、
跨域资源或登录POST。BOSS/51job暂不支持native。未知业务请求返回明确的
`native_operation_unreviewed`，不是“0岗位”。

D03 #49 将根据真实获准的页面/响应证据补首站专用业务契约与字段提取。D02原生框架
和人工本地TLS链路通过，不等于猎聘的完整搜索/登录/正文通过。真实登录SSO、未知
CDN、跨源跳转、需要后台worker的页面仍可能停止，并需逐项维护。不得为凑成功取消
正文完整性标准、改用Greenhouse或手工粘贴冒充三站成功。

## 验证与证据

- `python scripts/run_tests.py --report acceptance/results.json`：原测试及新增原生单测。
- `python scripts/run_native_browser_acceptance.py`：默认零请求，仅显示使用说明。
- 开发者显式 `--controlled`：应用实际后端、CONNECT、人工TLS上游、业务POST、
  HttpOnly Cookie、gzip、原报告；并验证重定向/禁止写操作/403/429/错误主机证书。
- Linux使用临时HOME/NSS测试CA；Windows仅GitHub CI环境临时在机器信任库加入随机测试CA并finally清除。
  这些是隔离人工上游测试工具，绝不作为产品的证书修复方案。
- 现有全部CI保留，新增独立Linux Chromium/Windows Edge原生验收；产物包含失败结果。

本执行容器现场尝试在人工robots导航上返回`ERR_BLOCKED_BY_ADMINISTRATOR`，
目标人工站收到0请求/0TLS连接。没有修改环境保护，也不将此记录算成原生浏览器通过。
最终本head CI及原始产物以PR实时核验结果为准，不在源码写固定的未来成功数。
用户本机同机同网对照与真实招聘正文仍待取得。

## 回滚

关闭原生选择，旧任务/默认bridge继续可用；不得在上游拒绝后以回滚自动重放。
无数据库迁移，无新的生产依赖；可选Playwright保持。内存响应随会话关闭清除，
保留已有岗位、私人证据、报告、历史配额。持久登录由D04单独交付。

依据：[CDP Fetch](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/)、
[CDP Target](https://chromedevtools.github.io/devtools-protocol/tot/Target/)、
[Playwright 路由](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route)、
[RFC9309](https://www.rfc-editor.org/rfc/rfc9309.html)。
