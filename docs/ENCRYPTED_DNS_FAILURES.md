# 普通加密 DNS 通信失败：保留当时的有限事实

Refs #225、#210/G09、#23、#223。#223 首次处理在 robots 请求前返回 encrypted_dns_unavailable；之后的独立 DNS 检查和实采成功，不能还原首次异常的阶段或原因。该旧失败的原异常已经丢弃，本修复不会补造它。

## 现有入口中的证据

原网络检查结果新增 effective_resolution.transport_diagnostic，公开 URL 采集的 fetch_diagnostic 新增 resolution_failure。两者都来自原 PublicResolver._exchange 已经发生的失败，没有新增探测。原 TLS 专用诊断继续使用 [DNS_TLS_FAILURE.md](DNS_TLS_FAILURE.md) 的独立字段。

仅保存固定类别/异常类型、当前操作阶段、观察时间、策略摘要、允许范围的整数 errno/winerror，以及最多四条固定 bootstrap 的 ip/phase/outcome。输出会重新构造，不复制异常字符串、自定义异常类名、查询域名、响应头/体、凭据、路径或任意上游建议。

| 阶段 | 可据此确定的范围 |
| --- | --- |
| tls_context | 连接对象或 TLS 环境准备处出错，不能说已连接 |
| connection_setup | 原 connect 尚未返回时中断；可能涉及 TCP、代理、TLS，不能仅凭阶段定位设备 |
| request_setup | connect 已返回，尚未进入 DNS 发送；设置剩余超时时间等发送前准备处中断 |
| dns_request | 已进入发送操作，不保证请求已完整发送 |
| response_headers | 已进入读取响应头操作 |
| response_body | 已进入读取 DNS 响应体操作 |

tls_handshake_completed=true 表示 connect 已返回，包括随后设置超时的 request_setup 阶段；该阶段的 dns_request_attempted 仍为 false。false 表示此标记未被证据确认。dns_request_attempted 同样是到达操作入口，不能当作完整发送或接收证明。source_request_started=false 仅指依赖此次解析的当前来源请求尚未发出；不代表整个批次/重定向链从未发过其他请求。原 http_attempts 仍是传输入口计数，不改成实际已发送请求数。

cause_confirmed 始终为 false。类型和数字错误码是线索，不定位网络设备、解析服务或中间代理的责任，也不建议据此安装证书。界面沿用现有网络检查结果展示；普通通信失败不会触发原证书修复入口。

## 冷却、策略与失败语义

原 encrypted_dns_unavailable 在保护期内仍返回 encrypted_dns_cooldown。重复读取沿用原 observed_at，reused_failure=true，不重连、不追加解析额度；策略变化会标 matches_current_policy=false，明确旧证据不证明新路线已经失败。撤销清理证据、取消仍阻断请求，均不刷新限额。失败恢复后的正常解析不会带上上一次失败信息。

保留10 秒解析预算、A/AAAA 顺序、30 秒保护期、允许路线、TLS 校验、重试及站点配额。固定长度响应在 EOF 时仍有声明的未读字节，会在解析 DNS 之前归为 IncompleteRead 的 encrypted_dns_unavailable，随后沿用原 transient 冷却；旧代码会将这种中断误归为 encrypted_dns_invalid_response。完整收到但内容无效的 DNS 响应仍保持原硬失败，不接受或解析截断正文。可选诊断本身失败时仍抛出原失败；不会把没有 JD 的批次改记成功或生成岗位报告。

## 验证范围

先在父提交上用人工连接/发送/响应头/响应体异常复现四处证据丢失，再在修复后对照原请求次数、预算及关闭行为。新增回归覆盖字段过滤、畸形数据、旧证据隔离、冷却/策略/撤销/取消，并贯穿原 SafeHTTP/SiteFetcher、Collector URL 路径和 GuidedService 网络检查入口。所有本项夹具使用人工异常并禁止外部网络；原 TLS 测试继续独立覆盖证书校验。

另有三个边界回归：connect 返回后设置超时异常、固定长度正文提前 EOF，以及完整或未声明长度但内容无效的 DNS 响应。前两类用旧代码复现三处断言失败，修复后通过；最后一类确认原硬失败分类保留。响应夹具使用标准库 HTTPResponse 读取本地字节，无外部网络。

这些是故障可观察性与响应完整性的回归，不能替代真实网络兼容性、正常账号搜索或目标网站验收；#223 首次网络失败及 #126 Windows 慢测试根因仍开放。

当前主干移植仅使用人工连接异常核验这些字段与原调用次数；历史实站观察保持原日期，不能据此认定旧失败根因已经修复。候选及主干需要各自首次完整CI和原程序产物验收；网络路径文件会触发原有固定公开目录工作流，其结果另行记录。
