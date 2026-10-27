# 网络与浏览器

[返回文档目录](../README.md)

先按当前出现的问题选择诊断或配置指南。具体企业代理和真实站点仍需环境验收；已支持协议不代表所有网络组合均已认证。

## 浏览器选择与启动诊断

- [DNS 已通过，但“浏览器组件未就绪”：逐步修复](../BROWSER_SETUP.md)
- [已安装但仍崩溃：不再循环重装](../BROWSER_CHOICE_AND_HISTORY.md)
- [本机 Chrome 的两种采集检查](../INSTALLED_CHROME_NATIVE.md)
- [本机取数前置故障：浏览器崩溃与 Fake-IP 诊断分开解决](../DESKTOP_STARTUP_RECOVERY.md)
- [固定空白页的就绪条件与失败阶段](../BROWSER_BLANK_CHECK.md)
- [浏览器就绪修复的合并前复核](../BROWSER_DIAGNOSTIC_REVIEW.md)
- [实际进程中的原生组件检查](../NATIVE_COMPONENT_CHECK.md)

## 代理配置、认证与系统凭据

- [网络兼容：已合并范围与仍待验收的缺口](../NETWORK_COMPATIBILITY_STATUS.md)
- [工作区网页代理偏好](../WORKSPACE_PROXY_SETTINGS.md)
- [工作区内的本机代理凭据](../WORKSPACE_PROXY_CREDENTIALS.md)
- [明确配置的本机代理认证（#75 / N5）](../LOCAL_PROXY_AUTH.md)
- [显式 HTTP Digest 本机代理（#385 / N5）](../HTTP_DIGEST_PROXY.md)
- [明确选择 NTLMv2 本机代理](../NTLMV2_PROXY.md)
- [合并 main 后的第一轮：SOCKS5 与统一策略整合](../SOCKS_POLICY_INTEGRATION.md)
- [显式宿主机代理入口](../VM_HOST_PROXY.md)
- [系统密钥库与工作区凭据（0.12.0）](../SYSTEM_CREDENTIALS.md)

## PAC 与 Windows 系统网络

- [跨平台域名 PAC](../PORTABLE_DOMAIN_PAC.md)
- [明确导入完整 HTTPS 网址 PAC（#23 / N6）](../FULL_URL_PAC.md)
- [Windows 本地 PAC：明确导入，按域名选择路线](../WINDOWS_DOMAIN_PAC.md)
- [Windows 已配置 PAC：确认来源后按会话读取](../WINDOWS_SYSTEM_PAC.md)
- [Windows WPAD：先发现，再确认来源（#23/N6）](../WINDOWS_WPAD.md)
- [Windows verify_code 20：修复构链方式，而不是重装浏览器](../WINDOWS_CERTIFICATE_CHAIN.md)

## DNS、TLS 和访问策略

- [公共域名加密解析（#23 / N5）](../PUBLIC_DNS.md)
- [Fake-IP：本机可选加密解析（#23 / #3）](../FAKE_IP_RESOLUTION.md)
- [普通加密 DNS 通信失败：保留当时的有限事实](../ENCRYPTED_DNS_FAILURES.md)
- [Edge 已启动，但 Fake-IP 解析仍失败：同一条因果链，不是反复重装](../DNS_TLS_FAILURE.md)
- [HTTP与浏览器使用同一规则匹配](../SHARED_ROBOTS_RULES.md)
- [robots 文件不存在时继续已适配的正常访问](../ROBOTS_STATUS_HANDLING.md)
- [robots 前导通配符](../LEADING_WILDCARD_ROBOTS.md)
- [检查通过而采集仍报 non_public_address：浏览器回调策略绑定](../BROWSER_POLICY_BINDING.md)
- [高级采集使用工作区网络偏好（#64）](../ADVANCED_COLLECTION_NETWORK.md)
- [高级站点HTTP的共享限额](../ADVANCED_SHARED_RATE.md)
- [先在本机跑通：公开岗位查询，不依赖自建服务](../LOCAL_FIRST_PUBLIC_DATA.md)
