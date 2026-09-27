# Edge 已启动，但 Fake-IP 解析仍失败：同一条因果链，不是反复重装

基线：PR40 合并后的 main@73e83a637d87a9b3a95627f0f9ccf81c37402f86。
用户现场已经得到 Edge `browser_ready`；随后 BOSS 查询的系统 DNS 为 198.18/15，工作区已同意加密解析，但诊断返回 `encrypted_dns_tls_failed`。截图任务仍显示先前的 `non_public_address`。不同阶段、会话或检查时间的错误不能拼成同一次请求的结论。

## 已确定与尚未确定

- 当前固定加密解析端点为 `cloudflare-dns.com:443/dns-query`，使用已配置的公开 bootstrap 地址、原目标主机对应的路由，保留 SNI、证书及主机名校验。不是对 `www.zhipin.com` 的证书检查；目标 TLS/HTTP 仍未被这份诊断验证。
- `mode=auto/source=system_route` 表示 Python 没有选择应用层显式静态代理，连接仍经过操作系统路由。不能据此判断 TUN/VPN 未运行，也不保证与日常浏览器的代理/PAC路线相同。
- 旧 `_exchange` 把所有 `ssl.SSLError` 压成同一个码并丢掉细节。证书链失败、主机名不符、握手警报、协议错误和连接提前关闭都可能出现这个总类错误，不能猜测根证书缺失、平台封禁或 VPN 产品故障。
- 采集请求桥遇到网络验证错误会主动 `route.abort('blockedbyclient')`。Edge 的 ERR_BLOCKED_BY_CLIENT 是这一动作的可能表现；截图中配套任务的网络错误支持此解释，不等于站点返回了 403，也不是让用户关闭 Edge 防护的依据。
- 新同意或路由设置用于新会话；旧任务错误是原观察。不要用旧任务记录替代新诊断。先通过当前网络检查，再在原任务中明确恢复；若仍绑定旧会话，先由用户停止该会话，保留数据/配额后重新执行。

## 本轮实现：保留失败证据，不改变访问能力

`effective_resolution.tls_diagnostic` 给出原操作的阶段、异常类别、允许的 SSL reason/library、证书验证数字码、固定解析端点、有限 bootstrap 尝试元数据、原 SSLContext 的验证策略/证书数量及 OpenSSL 版本。环境证书路径只表示是否设置，不保存路径值。原始异常文本、verify_message（可能带主机名）、证书正文、系统根证书清单、请求域名历史、HTTP头/密码/Cookie均不复制。

`cause_confirmed=false` 保持：上述字段可缩小问题，但不识别造成故障的设备或操作者。只有握手实际完成才标记 `tls_handshake_completed=true`；`dns_request_attempted` 是是否尝试发送 POST，不保证已完整发送。不存在的新连接不能假报成功。

原30秒失败等待期继续生效，重复点击保留原错误类别并返回相同时间的证据，`reused_failure=true`，不会追加连接。策略发生改变时标记证据是否匹配，不把旧路线的失败当成新路线已测试。撤销清除证据但不重置额度或错误保护。

任务页面对 DNS 类失败明确解释本程序主动中止请求及浏览器通用错误码，原始任务错误仍保留。

## 用户操作与修复分支

更新本次代码后，保持已可用的 Edge，选本次实际平台（现场是 BOSS，不是猎聘），点击原“检查当前网络策略”。不用安装依赖、改环境变量、运行多条命令或再上传整份浏览器安装日志。若提示显示历史失败，等原等待期结束再确认一次；不清空工作区或反复新建任务。

| 分类 | 应核对什么 | 不应直接做什么 |
| --- | --- | --- |
| certificate_verification | verify_code 与 verification_reason：时钟/有效期、主机名、可信证书链分别核对。Python SSLContext 的实际计数与验证设置也保留。 | 不因看到 ssl 就安装 certifi；不自动导入收到的证书，不关闭校验。 |
| peer_closed | 核对当前允许的网络路线、TLS中间设备及 SSL 原因；远端或路径关闭只是线索，尚不能确定责任方。 | 不把 EOF 当作证书过期，不重装浏览器。 |
| protocol_mismatch | 当前已有代理/路由是否把 TLS 连接送到了不同协议的服务，端口/协议是否正确。 | 不猜端口扫描，不自动换出口。 |
| tls_protocol | 按 SSL reason 检查协商/读取阶段；必要时由网络管理员核对。 | 不盲目降级 TLS 或改系统策略。 |

如果网络管理员明确禁止该解析服务，停止此路径，由管理员批准合适接入方式；本轮不换解析商绕过限制。若现有用户授权配置存在错误，按精确错误修正该配置后，仍需原 TLS 校验通过。`effective_dns_ok` 也只证明解析，不是招聘网站全部可用。

## 测试与验收范围

新增错误类型/隐私/阶段/冷却/撤销/策略变化/原请求桥中止回归；原本机 TLS 测试服务补充真实未受信证书的失败证据与无POST/无目标请求断言。原桌面浏览器测试增加同一按钮的可视诊断，保留严格CSP、所有系统/SDK矩阵和之前成功/失败断言。模拟异常不能代替用户本机 TLS 结果。

这次没有修改证书信任源、TLS参数、连接候选、代理选择、DoH提供者、重试范围、原配额、`route.abort` 或招聘站请求规则。也没有将旧高级批次接线纳入本轮。只有诊断信息丢失与拦截来源提示这两个已确定的产品缺陷被修复，不宣称用户 TLS 已成功或三站已认证。

官方参考：
- https://docs.python.org/3.12/library/ssl.html （SSLCertVerificationError 与 Windows 默认根证书加载）
- https://playwright.dev/python/docs/api/class-route#route-abort （blockedbyclient）
- https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/ （DoH端点与TLS）

普通 OSError/HTTPException 的允许字段、公开采集透传与冷却证据见 [普通加密 DNS 通信失败](ENCRYPTED_DNS_FAILURES.md)（#225）；保持原 TLS 分类和修复条件。
