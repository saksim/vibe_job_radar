# 当前取数能力与验证范围

核对日期 2026-09-23，已合并主干基线 `ec458799`（PR61）。本表由 `acquisition_status.py` 生成，同一份数据进入适配注册表、采集页与新报告的 `software_acquisition_capabilities`。修改后执行 `python scripts/check_acquisition_status.py --write` 并核对证据；自动测试检查文字表格未与程序脱节。

<!-- acquisition-status:start -->
| 平台 / 适配版本 | 后端 / 默认 | 实现 | 最近记录的受控验证 | 实站 / 剩余跟踪 |
|---|---|---|---|---|
| BOSS直聘 / 1 | bridge / 默认 | implemented | [2026-09-18 人工夹具](https://github.com/saksim/vibe_job_radar/actions/runs/35358529040) | not_verified；[#55](https://github.com/saksim/vibe_job_radar/issues/55) |
| BOSS直聘 / 1 | native / 不可用 | blocked | 无匹配记录 | not_verified；[#55](https://github.com/saksim/vibe_job_radar/issues/55) |
| 猎聘 / 3 | bridge / 默认 | implemented | [2026-09-23 人工夹具](https://github.com/saksim/vibe_job_radar/actions/runs/35891758001) | blocked；[#54](https://github.com/saksim/vibe_job_radar/issues/54) / [#63](https://github.com/saksim/vibe_job_radar/issues/63) |
| 猎聘 / 3 | native / 显式实验 | implemented | [2026-09-23 人工夹具](https://github.com/saksim/vibe_job_radar/actions/runs/35891758054) | blocked；[#54](https://github.com/saksim/vibe_job_radar/issues/54) / [#63](https://github.com/saksim/vibe_job_radar/issues/63) |
| 前程无忧 / 1 | bridge / 默认 | implemented | [2026-09-18 人工夹具](https://github.com/saksim/vibe_job_radar/actions/runs/35358529040) | not_verified；[#56](https://github.com/saksim/vibe_job_radar/issues/56) |
| 前程无忧 / 1 | native / 不可用 | blocked | 无匹配记录 | not_verified；[#56](https://github.com/saksim/vibe_job_radar/issues/56) |
<!-- acquisition-status:end -->

表中的受控验证记录绑定历史源码 `cde1e1c`、适配定义与访问契约，保留对应 CI、日期、浏览器/OS、网络和人工页面范围。它不证明修改后的全部代码、当前用户环境或真实平台。定义变化会撤下匹配的受控记录，不能只沿用同名平台的旧成功。报告中的软件快照不改变某条岗位的来源方式或证据等级；真实正文仍以逐条原文、采集时间和来源链判断。

`implemented` 表示有实现；`controlled_verified` 表示注明范围的人工环境曾验过；`pilot_verified` 应有单独实际环境证据；`live_verified` 应达到对应站点样本、日期和恢复要求；`blocked` 表示条件受阻。**当前三站均没有 pilot/live 认证。** 后端是否允许选择、是否默认和是否实站成功分别记录，选择默认浏览器桥不代表网站已能抓取。

## 主干已经具备的流程

工作台目标岗位 → 平台与检索词 → 独立浏览器正常人工登录 → 程序读取列表/分页 → 选择详情 → 保存独立正文与最终 URL → 原分析/报告 → 本人证据。已有 JD 可以粘贴/导入；Brave、获准 URL 与约定 JSON 入口保留。公开 Anthropic 目录用于其明确来源范围，不替代三站。

PR60 已合并人工登录返回后的自动接续、当前会话复用与完整 JD 到报告。PR61 已合并明确选择的工作区 Cookie 保存/恢复；默认不持久保存，不读取日常浏览器。Windows 使用当前用户 DPAPI，Linux/macOS 使用仅所有者权限文件且不加密。密码、localStorage、IndexedDB 不保存；Cookie 恢复不证明账号仍有效。见 [本机会话说明](SAVED_SESSION_D04.md)。

原生实验已合并，但主干猎聘契约仍限 bootstrap，未知业务、SSO 或跨域行为不能通过开关自动获得支持。BOSS/51job 没有原生契约。真实猎聘搜索入口受 robots 及页面跳转空白影响，见 [#63](https://github.com/saksim/vibe_job_radar/issues/63)，未取得本系列真实账号成功及完整 JD 验收。

## 待合并实现与实站缺口

- [PR62](https://github.com/saksim/vibe_job_radar/pull/62)：猎聘单次正常密码表单、自动接续、小批身份去重、查询范围与检查点、逐条取数漏斗。代码和受控测试结果以该 PR 当前 head 为准；未合并，不写成主干已支持密码自动填写。
- [PR66](https://github.com/saksim/vibe_job_radar/pull/66)：高级采集/CLI 工作区网络偏好接线。
- [PR67](https://github.com/saksim/vibe_job_radar/pull/67)：Windows 拒绝 POST 时可靠返回错误。
- [PR69](https://github.com/saksim/vibe_job_radar/pull/69)：公开任务停止和重启后确认继续。

三站真实正常登录、首条/小批完整 JD、跨日期至少 30 条/3 日期、过期与结构变化现场恢复仍须逐项验收。无 href、特殊 iframe/SSO/业务响应需专用契约。PAC、代理认证、VM宿主机、所有 VPN/TUN 产品、长期调度和跨设备账户配额均未完成。完整清单见 [交付状态](DELIVERY_STATUS.md)。

## 数据与回退

本矩阵是附加元数据，不改 jobs.sqlite 的 schema 1、原文、已有报告、个人证据、配额和会话格式，不自动迁移历史报告。升级/回退实测与操作见 [工作区兼容验证](WORKSPACE_COMPATIBILITY.md)。不同旧版本对 `browser_fetch` 等来源方式的支持不能一概而论，必须使用对应源码和完整备份核验。

[按钮指南](GUIDED_COLLECTION.md) · [访问契约](NATIVE_BROWSER_D02.md) · [网络状态](NETWORK_COMPATIBILITY_STATUS.md)
