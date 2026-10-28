# 猎聘网络失败归属与首次匿名搜索（2026-10-28，#531）

本轮从 dev `19ead093dfa2c928d015d6e5ae270f2d80a98320`（PR #536）继续，范围固定为 #531 的有限网络失败诊断和一次匿名页面/搜索检查。已有 CDP 接入、城市操作规则、登录与会话工程不重做；#532/#533/#534 不在本轮实现范围内。

## 修正的问题

上一轮的 `network_error` 没有保留失败请求的归属和浏览器原因；HTTP 200 又可能只是响应头，不能证明正文完成。本轮将已有开发检查的有限字段过滤提取到运行时共用函数，使正式采集路径也能在删除请求记录前，将首次停止绑定到该请求的主机、脱敏路径、方法、资源类型和已核对规则。

- 记录固定 CDP 网络错误、阻止原因、CORS 原因及布尔取消标记；未知值保持 `unknown`，缺失的可选结构保持空值。分类值参考[官方 CDP Network 协议](https://github.com/ChromeDevTools/devtools-protocol/blob/master/json/browser_protocol.json)，兼容已有旧版分类。
- 阶段只区分 `before_response`、`response_headers` 和 `body_complete`；后者仅表示收到 `Network.loadingFinished`，不等于正文已解析、岗位可读或账号认证成功。
- 不记录任意错误文本、请求头/正文、查询值或 CORS `failedParameter`。保持原诊断开关、环形缓冲、首次停止保存及观察器容错。
- 原公共错误码、拒绝/取消决定、请求频率、网络契约和浏览器行为不变；不增加浏览器命令、重试或自动切换。

## 首次实站结果

候选代码 `b3a988ec0d3b18e4cdb68b156d05a8e2768f3ba5`，Windows、Python 3.11.5、程序专用 Edge 156.0.4314.50、原生 CDP 有界面入口。沿用原工作区网络策略和共享配额，使用全新匿名上下文。正常用户执行环境沿用 PR #535 已确认的启动条件。

预先固定流程为正常打开搜索入口、观察至少 60 秒、确认搜索入口可用后只提交一次原任务关键词、核对解析结果；不直接构造平台 API 或进行登录。本次唯一实站检查结果：

| 观察项 | 首次结果 |
| --- | --- |
| 页面观察 | 60.172 秒，未跳 `about:blank`，搜索入口可用 |
| 原关键词搜索 | 提交一次，解析到 5 个职位候选 |
| 城市目录 GET | 观察到 HTTP 200 和 `body_complete` |
| 城市编码查询 OPTIONS / POST | 均观察到 HTTP 200 和 `body_complete` |
| 搜索 POST | 观察到 HTTP 200 和 `body_complete`；候选解析另外核对 |
| 首次后端停止 | `null`；此前 `network_error` 本次未复现 |
| 原任务 | 文件逐字节保持，未修改状态/选择/正文 |
| 账号及最终产出 | 未登录、未发短信、未保存会话；完整 JD 0，报告未生成 |

`first_backend_stop=null` 不表示页面的每个请求成功：原策略继续阻止可选资源，另有未终止搜索的 DNS 解析失败事件。诊断环形缓冲丢弃 267 条较早事件、观察器错误 0；首次失败及首次后端停止独立保留，不能把剩余事件当完整网络日志。

清理阶段抛出 `RuntimeError`。快照观察到浏览器断开、进程退出、临时 profile 删除、隧道关闭及主线程停止；但该快照不涵盖所有未完成处理器，且只保存异常类型，无法据此判定异常来源或宣称完整清理通过。保留该未解决事实，不把匿名搜索成功扩大为全生命周期验收成功。

本机忽略目录 `.verify/liepin-network-20261028/` 保留首次结果和运行入口，不上传原始事件：

| 文件 | SHA-256 |
| --- | --- |
| `diagnosis-first.json` | `d40cb428b07acf10201593120f15bce9909062e59dfe8feab0e5a0d2532608ca` |
| `check_public_once.py` | `205cb25ba22517b38125d08bf051f4df57ebd8b6ef7ca94d6e606b97a3d168d5` |

入口独占保留各阶段首次文件并拒绝隐式重跑。由于此前网络失败未复现，本轮没有据此修改传输行为，也没有启动第二次实站验收。此前失败证据仍见 [城市编码依赖记录](LIEPIN_REGION_LOOKUP.md)，不以本次成功覆盖。

## 验证及后续

本地 154 项定向回归一次通过，覆盖两种控制路径的请求归属、200 响应头后 CORS 失败、正文完成但尚未解析、未知字段/敏感字段过滤、首次停止不被后续噪声覆盖、取消和可选请求、观察器异常，以及既有规则/采集/节流行为。命令：

```powershell
$env:PYTHONPATH='src;tests'
python -m unittest test_native_network_diagnostics test_native_failure_classification test_native_failure_evidence test_native_acquisition test_acquisition_diagnostics test_native_contract_diagnostics test_native_cancel_ack test_native_request_pacing
```

差异空白检查通过。既有 CI 的结果在 PR/#50 收尾记录，不新增矩阵、不手动扩大检查、不以合成检查替代真实登录。

#531 的匿名页面和搜索门槛本次有了匹配代码的正面证据，但旧 `network_error` 原因与本次退出异常仍未知，#531/#50 暂保持开放。下一步先给清理过程补有限的阶段/资源归属，保留首次异常；可据已通过的页面条件推进 #532 的反馈及原任务交接离线准备。真实短信验收应在退出异常解决且交接反馈就绪后进行，再接续 #49 的完整 JD/报告及 #533 的真实会话复用。#534 条件路线继续延期。
