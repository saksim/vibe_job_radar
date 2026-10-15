# 明确导入完整 HTTPS 网址 PAC（#23 / N6）

需要按职位路径或查询参数分流时，可显式选择此模式。原 Windows 域名 PAC、系统 PAC 地址和 WPAD 仍只接收 HTTPS 根地址，原许可不会扩大。本模式只读取用户导入的可信本地脚本，不下载系统 PAC、不发现网络、不修改系统设置。

## 操作和适用入口

在首页、引导页或高级页展开“网络自动适配”，将本地 PAC 范围改为“完整网址（HTTP与请求桥）”。阅读说明，选择 UTF-8 的 .pac/.js 文件（最多64KiB），重新勾选同意后导入。切换范围或文件会清除旧勾选；导入和状态刷新不执行脚本。

源码环境需要单独安装固定的可选组件：在项目目录用所选解释器执行 `python -m pip install ".[pac]"`。组件为 quickjs-ng 0.17.0.1；缺失或版本不符时页面说明不可用，程序不会自行安装。Windows便携候选包收集该组件，实际冻结进程是否通过须查看同一提交的验收记录。

该模式适用于 HTTP 采集和浏览器请求桥。原生浏览器的 CONNECT 入口只有主机和端口，无法得到 TLS 内部的路径；选择此模式后，原生登录、搜索和采集会明确停止，在创建浏览器、目标 DNS 或消耗登录次数之前拒绝。若需要原生浏览器，须显式选择其支持的其他网络模式。本功能没有 TLS 解密中间人，也不自动换通道。

“检查脚本”只把 `https://pac-check.invalid/path?probe=explicit` 与 `pac-check.invalid` 交给脚本。不会连接返回的代理或目标；通过只说明这条人工输入获得受支持的结果，不代表招聘站点、真实企业网络或正常账号链路通过。

## 传入内容与支持的脚本

`FindProxyForURL(url, host)` 收到规范的完整 HTTPS 网址和域名。域名转为小写，省略默认443端口，空路径变为 `/`；百分号编码的路径与查询参数保持原样。不接受 HTTP、其他端口、userinfo、片段、空白、反斜杠或超过8192字符的地址；非ASCII路径应先正确编码。

**路径和查询参数可能包含搜索词或敏感值。** 仅导入自己信任的脚本。本模式不传 Cookie、请求头、正文或个人材料，也不把网址原文放入选路缓存、公开状态或错误信息。它不是面向恶意原生引擎漏洞的安全沙箱。

独立进程中的 JavaScript 环境提供五个字符串助手：

- `isPlainHostName`
- `dnsDomainIs`
- `localHostOrDomainIs`
- `dnsDomainLevels`
- `shExpMatch`，其中 `*` 和 `?` 为通配符

另提供 `weekdayRange`、`dateRange`、`timeRange` 三个纯日期时间助手，读取引擎的本地时间；末尾的 `"GMT"` 改用 UTC。每次调用只读取一个时刻，比较精度取决于传入的参数，首尾均包含：

| 助手 | 支持的参数（末尾均可加 `"GMT"`） |
|---|---|
| `weekdayRange` | 一个星期名，或起止星期名；使用 `SUN` 至 `SAT` |
| `dateRange` | 单个日/月/年；同类的两个值；`日,月,日,月`；`月,年,月,年`；`日,月,年,日,月,年` |
| `timeRange` | 一个小时；起止小时；`时,分,时,分`；`时,分,秒,时,分,秒` |

月份使用大写 `JAN` 至 `DEC`。数值须为整数：日1—31、年份32—9999、小时0—23、分/秒0—59。实际月日组合必须有效；不带年份的2月29日作为每年模式，仅在真正的闰日匹配。

周期范围逆序时跨越周期边界，例如 `weekdayRange("FRI","MON")` 包含周末，`timeRange(23,1)` 包含午夜。带年份的绝对范围必须从早到晚。结束小时包含该小时的全部时间，结束分钟/秒同理；例如 `timeRange(12,13)` 包含13点这一整小时。混合的两个日期参数（例如日+月）、无效日期、非法个数/类型和倒序绝对日期会停止脚本，不猜测含义。

这些是本项目明确支持的规则。参考 [PAC接口说明](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Proxy_servers_and_tunneling/Proxy_Auto-Configuration_PAC_file)和[Chromium实现](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/services/proxy_resolver/pac_js_library.h)时，须注意旧浏览器对逆序小时、容错转换和结束精度存在差异；不承诺所有浏览器的历史行为一致。

**时间函数仍受会话缓存约束。** 每个规范网址在当前会话仅采用首次求值结果，跨过时段边界不会自动重新选路。需要按新时刻重新求值时，停止并新开会话；旧会话不会因升级而重新发送请求。

原“完整网址”模式没有 Python 回调、网络、文件、DNS 或本机地址接口。在原模式调用 `dnsResolve`、`isInNet`、`myIpAddress` 会停止选路，原许可不会获得新能力。新增助手只组合本来可用的 JavaScript 日期能力，许可的数据范围、原授权标识和会话缓存保持。

返回值沿用原严格规则：最多96个ASCII字符、至多4项，完整校验每项，只使用第一项。支持 DIRECT、匿名本机 PROXY、明确的匿名本机 SOCKS5；不支持认证、远程代理、宿主机地址、socks5h 或模糊的 SOCKS。连接失败不继续下一项或直连。

## 0.5.0：另行同意 DNS 助手

三个页面均增加“完整网址 + DNS助手”。切换范围必须重新同意，使用 schema 7、pac_url_dns 和 workspace-trusted-full-https-url-pac-dns-v1 独立许可；旧 schema 6 仍无DNS能力。导入、状态读取和保存不查询DNS，显式“检查脚本”或实际选路才执行。

新增 dnsResolve、isResolvable、IPv4 isInNet 和 myIpAddress。DNS由隔离进程通过系统解析器完成，每次求值最多16个不同规范名称（含失败），同次结果缓存；IPv4常量不查询。名称不存在返回null/false，传输故障、超预算或脚本超时停止选路，不能吞掉错误后静默直连。myIpAddress取本机主机名解析到的首个IPv4，无地址时返回127.0.0.1；不扫描网卡、不保证多网卡环境中的特定出口。

**可信脚本可向系统DNS提交自选名称，并读取本机IPv4。** 系统解析器可能使用本地缓存、hosts或已配置DNS服务，DNS服务可见脚本查询名称。此模式不属于公共加密DNS承诺；企业分区DNS需现场核对。参考[MDN PAC助手说明](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Proxy_servers_and_tunneling/Proxy_Auto-Configuration_PAC_file)。

DNS辅助仅决定路线；实际HTTPS请求仍独立验证公网目标、TLS和来源权限，不因PAC返回私网地址而放开目标访问。支持IPv4助手，不含IPv6扩展、socks5h或代理端DNS；原生CONNECT入口仍不支持完整网址模式。

## 进程、缓存与撤销

使用固定 QuickJS-NG 版本的短命应用自有进程；JS堆上限16MiB、栈上限512KiB、原无DNS模式单次引擎时间限制4秒。DNS模式使用宿主回调，不启用与回调不兼容的引擎CPU计时，仍由隔离父进程的共同期限终止阻塞DNS或死循环。父进程从启动前开始计算8秒共同期限，成功结果在接收和最后许可检查之后仍须未超时，最多两个并发进程。线程调度、进程启动与回收可能延迟实际返回，不承诺整个调用严格在8秒内结束。

同一会话对每个规范网址只求值一次，包括固定错误；最多128个不同网址。缓存键仅为网址摘要，脚本副本和每次导入的许可固定。更换配置、改动/丢失文件、取消任务或关闭工作区后，旧缓存和已选路线不能建立新连接；已发出的请求或已有连接无法撤回。超时、脚本错误、进程异常都停止，不自动重试。

公共加密解析沿本次目标网址选出的路线，不会用解析商网址重新执行 PAC。同一域名、同一许可、相同代理协议/端点可以复用尚有效的 DNS 回答；不同协议或端点隔离缓存。保留原解析预算、冷却、公网地址校验、原目标 SNI/TLS、robots 与访问频次。

## 保存与回退

偏好使用 schema 6、`pac_url` 模式和独立的 `workspace-trusted-full-https-url-pac-v1` 许可，脚本仍保存为工作区 `network-pac/<sha256>.js`。更改 DNS 范围保留当前 PAC 授权；重新导入即使脚本相同也撤销旧会话。

旧源码会拒绝未知 schema，不会静默直连。回退前停止任务，在当前版本显式选择自动或固定代理并保存，确认网络偏好回到 schema 2，再按[工作区兼容说明](WORKSPACE_COMPATIBILITY.md)完整备份并回退。原脚本副本、报告、数据库和配额保留。

## 验收边界

受控测试覆盖同域名不同路径/参数分流、真实本机 HTTP/SOCKS→TLS、解析路线与缓存、无隐式执行、独立同意/撤销、异常与资源上限、原生操作在额度预留前拒绝。三页真实浏览器验证范围切换、导入、重载、显式检查和390px界面；便携门槛另要求实际冻结子进程证明完整输入、未提供的助手拒绝以及撤销。

精确候选与合并后 main 的首次 CI/程序包结果分别记录在对应 PR 和 [Issue #23](https://github.com/saksim/vibe_job_radar/issues/23)，不能继承其他提交的成功。实际企业 PAC、DHCP提供地址、其他代理认证、VPN产品矩阵及三站账号现场仍开放。

组件来源为 [quickjs-ng 0.17.0.1](https://pypi.org/project/quickjs-ng/0.17.0.1/)；固定版本许可证及上游声明见 [随包声明](licenses/QUICKJS_NG_NOTICES.txt)。原生域名模式的差异见 [Windows 域名 PAC](WINDOWS_DOMAIN_PAC.md)。
