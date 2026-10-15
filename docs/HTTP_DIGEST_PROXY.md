# 显式 HTTP Digest 本机代理（#385 / N5）

0.8.0 在原 HTTP Basic、SOCKS5 用户名密码之外增加 HTTP Digest。三个工作台的“本工作区的代理”中选择“本机 HTTP Digest＋保存凭据”，输入可信的 loopback HTTP 入口和独立凭据后确认保存。Windows 使用原当前用户 DPAPI；保存不联网，页面不回显凭据。修改后停止并重开已有采集会话，清除凭据会停止该入口的新连接。

CLI 和其他系统可在当前进程中明确设置 VIBE_RADAR_PROXY_AUTH=digest，同时提供原 VIBE_RADAR_HTTP_PROXY、VIBE_RADAR_PROXY_USERNAME 和 VIBE_RADAR_PROXY_PASSWORD。该认证选项仅用于显式 HTTP 入口；缺少完整凭据、未知取值或与 SOCKS5 混用均停止。未设置认证选项时，已有 HTTP Basic 与 SOCKS5 行为保持。不把凭据交给系统自动发现、PAC、宿主机或其他入口。不要将真实密码放进命令历史、脚本、日志或版本库。

## 支持范围

依据 [RFC 7616](https://www.rfc-editor.org/rfc/rfc7616.html)，支持 MD5、SHA-256、SHA-512-256 及各自的 sess 变体、qop=auth、空 CONNECT 请求体的 auth-int，以及旧无 qop 格式。多个有效 Digest 选项优先使用支持的最强算法；不选择 Basic。算法受运行时加密库支持约束，不绕过系统加密策略。保留用户名/密码各 1–255 个可打印 ASCII 字符、用户名不含冒号的原契约；不推测非 ASCII 凭据编码。

第一次 CONNECT 不携带凭据，仅接受有效的 407 Digest 挑战；随后关闭此连接，向同一明确的 loopback 入口发送一次 Digest 响应。两次连接、收发和计算共用原期限；响应头至多 16 KiB，不读取挑战响应的大正文。随机 cnonce、nonce-count、opaque 和 userhash 按所选挑战处理；服务器若提供 rspauth 则验证。每条新隧道重新取挑战，不缓存 nonce。

这两步属于一次认证握手；原站请求仅在认证成功并验证原站 TLS 后发送一次。初次匿名成功、无有效 Digest、畸形回复、错误服务器证明、第二次拒绝（包括 stale）或超时均停止，不再认证、不改走匿名/Basic/直连。原生浏览器并发握手沿用原串行机制，一次拒绝或结果不明阻止同会话后续重复凭据。nonce 过期时需用户关闭旧会话并明确重新开始。

CONNECT 始终携带经过验证的数字公网 IPv4/IPv6 和 443 端口；网站仍使用原域名 SNI/TLS。Digest 不提供代理端 DNS，不解除私网、Fake-IP、来源范围、额度或 TLS 限制。代理认证信息不会进入原站 HTTP 头。HTTP Digest 也不加密本机握手；应使用可信本机代理与强密码，MD5 仅为旧代理互操作支持。

## 保存和回退

认证方式参与运行中策略、私有会话和后台任务的身份绑定。改用 Basic、修改入口或凭据都需要新会话；匿名和原 Basic/SOCKS5 身份格式保持。工作区密文额外绑定 Digest 模式；不能将旧 Basic 密文改个模式名后复用。

网络偏好继续使用 schema 8，新增 http_digest_auth 模式。旧版 0.6/0.7 的读取器会拒绝未知模式，不会当成匿名 HTTP。回退前先在新版停止任务、明确改回自动或原支持模式并保存，按工作区备份说明操作；只清除凭据仍保留停止的 Digest 模式。Linux/macOS 的加密保存和认证 Cookie 跨进程限制仍沿用原契约。

## 验证与未完成条件

RFC MD5/SHA-256 标准向量、独立服务器计算的六种算法变体、真实本机 TLS、IPv6 CONNECT、一次 POST、原生并发拒绝、同一期限、配置隔离和 Windows 独立进程 DPAPI 均有定向检查。已有网页请求桥、原生浏览器矩阵增加 Digest；三个真实工作台入口和实际冻结程序的保存/重启/清除另验。准确提交的实际结果见 #385 与对应 PR/发行记录，首次失败保留，不预填通过。

这交付一个明确协议范围；NTLM/Negotiate、其他系统凭据库、代理端 DNS 和真实企业/VPN 产品认证继续开放。用户尚未确定代理产品/协议，不把人工代理夹具冒充现场兼容。正常招聘账号登录仍由原专项处理。
