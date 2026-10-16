# 明确选择 NTLMv2 本机代理

0.10.0 新增独立选择的 NTLMv2 认证；是否已经正式发布，以 GitHub Release 和对应 Tag 为准。

## 使用

工作台的网络设置中选择“本机 HTTP NTLMv2＋保存凭据”，填写可信本机 HTTP 入口和代理账号，明确同意后保存。三个页面使用同一工作区设置；输入框保存后清空。域账号可以填写 `域\用户名`；当前用户名和密码沿用 1–255 个可打印 ASCII 字符的约定。不要填写招聘平台账号。

Windows 凭据由当前账号 DPAPI 加密；Linux/macOS 从0.12.0可使用[系统默认密钥库](SYSTEM_CREDENTIALS.md)。各自绑定工作区位置、入口和认证方式。修改、清除或切换模式后，应停止并重开原会话。清除后新连接停止，原任务、JD 和报告保留。显式环境入口继续保留，系统加密方式需对应平台组件和密钥库。

CLI 使用既有 `VIBE_RADAR_HTTP_PROXY`、`VIBE_RADAR_PROXY_USERNAME`、`VIBE_RADAR_PROXY_PASSWORD`，并将 `VIBE_RADAR_PROXY_AUTH` 明确设为 `ntlmv2`。凭据不写入代理 URL、日志或诊断；没有完整凭据或组件时停止。不要在公开 Issue 或命令示例中粘贴真实密码。

Python 安装方式需要本发行的 `ntlm` 可选依赖（`pyspnego==0.12.4`）；便携程序包含固定组件。可运行 `vibe-radar-ui --ntlm-component-check` 或 `VibeJobRadar.exe --ntlm-component-check` 离线检查组件和独立进程。检查只用人工凭据，不连接代理、招聘站点，也不验证你的实际账号。

## 连接范围

- 只使用明确指定的 loopback HTTP 代理，发送已验证公网数字 IP 的 CONNECT；随后仍按原目标域名验证 TLS。
- HTTP、浏览器请求桥和原生浏览器共用这一认证路线。身份变化使原会话失效；旧 Basic、Digest、SOCKS5 和全部 PAC 模式保留。
- 隔离进程强制 NTLMv2，使用提供的用户名密码，省略工作站名。不会读取系统登录身份、SSPI 缓存或外部凭据文件，也不自动协商 Negotiate/Kerberos。
- 一次 Type1/Type2/Type3 交换使用同一 TCP 连接，认证和传输共用原连接期限，最多两个认证进程并发。拒绝、超时或无效响应后关闭连接和进程，不重试认证或改走直连。
- 407 响应须保留连接，提供单一 Content-Length（正文最多 64 KiB）；分块、关闭定界、重复长度及不明确的挑战不在当前范围。挑战令牌最多 8 KiB，响应头最多 16 KiB。

## 保存与回退

NTLMv2 使用独立许可和 schema 11。0.9.0 及更早版本明确拒绝这一格式；降级前先在新版切回自动或旧模式，再启动旧版本。原 schema 8 的 Basic/Digest/SOCKS 凭据和原网络身份规则保持不变。

## 实际证据与剩余条件

子 Issue [#391](https://github.com/saksim/vibe_job_radar/issues/391) 记录对应 PR、首次失败和实际结果。本机人工代理/TLS 已验证同连接认证、错误密码拒绝、HTTP/桥/原生守卫和原站证书校验；本机 Edge 155.0.4283.45 通过三个页面设置及真实人工代理、证书、DPAPI 新浏览器会话测试；冻结程序结果须以对应提交的验收为准。冻结组件诊断只证明包内离线协议与进程清理，不能代替真实代理 TCP 或企业互操作。

真实企业/VPN、多网卡、Kerberos/Negotiate、代理端 DNS、其他系统凭据库及签名仍单列。用户尚不知道实际代理协议，因此不能宣称本模式已解决其未知网络问题。
