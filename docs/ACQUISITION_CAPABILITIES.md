# 当前取数能力与验证范围

文案核对日期 2026-10-08。本源码的流程与历史受控证据分别记录；准确提交的候选、主干和程序包资格见对应PR。本表由 acquisition_status.py 生成，同一份数据进入适配注册表、采集页与新报告的 software_acquisition_capabilities。修改后执行 python scripts/check_acquisition_status.py --write 并核对证据；自动测试检查文字表格未与程序脱节。

猎聘原生实验已有本机 Edge 正常关键词单页列表观察，但未完成正常账号到相关完整JD和原报告的验收。公开分类历史采样覆盖四个真实日期；较早第二日、第三日记录仍保留各自分母，不能用最新累计数改写旧报告。见[已有取数证据](CURRENT_ACQUISITION_EVIDENCE.md)。这些观察不认证默认浏览器桥、其他环境或其他平台。

<!-- acquisition-status:start -->
| 平台 / 适配版本 | 后端 / 默认 | 实现 | 最近记录的受控验证 | 实站 / 剩余跟踪 |
|---|---|---|---|---|
| BOSS直聘 / 1 | bridge / 默认 | implemented | [2026-09-18 人工夹具](https://github.com/saksim/vibe_job_radar/actions/runs/35358529040) | not_verified；[#55](https://github.com/saksim/vibe_job_radar/issues/55) |
| BOSS直聘 / 1 | native / 不可用 | blocked | 无匹配记录 | not_verified；[#55](https://github.com/saksim/vibe_job_radar/issues/55) |
| 猎聘 / 3 | bridge / 默认 | implemented | [2026-09-23 人工夹具](https://github.com/saksim/vibe_job_radar/actions/runs/35891758001) | blocked；[#54](https://github.com/saksim/vibe_job_radar/issues/54) / [#63](https://github.com/saksim/vibe_job_radar/issues/63) |
| 猎聘 / 3 | native / 显式实验 | implemented | 无匹配记录 | blocked；[#54](https://github.com/saksim/vibe_job_radar/issues/54) / [#63](https://github.com/saksim/vibe_job_radar/issues/63) |
| 前程无忧 / 1 | bridge / 默认 | implemented | [2026-09-18 人工夹具](https://github.com/saksim/vibe_job_radar/actions/runs/35358529040) | not_verified；[#56](https://github.com/saksim/vibe_job_radar/issues/56) |
| 前程无忧 / 1 | native / 不可用 | blocked | 无匹配记录 | not_verified；[#56](https://github.com/saksim/vibe_job_radar/issues/56) |
<!-- acquisition-status:end -->

表中的受控验证记录分别绑定历史源码 `cde1e1c`（2026-09-18）或猎聘定义 `0d85f554`（2026-09-23）、适配定义与访问契约，保留对应 CI、日期、浏览器/OS、网络和人工页面范围。它不证明修改后的全部代码、当前用户环境或真实平台。定义变化会撤下匹配的受控记录，不能只沿用同名平台的旧成功。报告中的软件快照不改变某条岗位的来源方式或证据等级；真实正文仍以逐条原文、采集时间和来源链判断。

`implemented` 表示有实现；`controlled_verified` 表示注明范围的人工环境曾验过；`pilot_verified` 应有单独实际环境证据；`live_verified` 应达到对应站点样本、日期和恢复要求；`blocked` 表示条件受阻。**当前三站均没有 pilot/live 认证。** 后端是否允许选择、是否默认和是否实站成功分别记录，选择默认浏览器桥不代表网站已能抓取。

## 主干已经具备的流程

工作台目标岗位 → 平台与检索词 → 独立浏览器正常人工登录 → 程序读取列表/分页 → 选择详情 → 保存独立正文与最终 URL → 原分析/报告 → 本人证据。已有 JD 可以粘贴/导入；Brave、获准 URL 与约定 JSON 入口保留。公开 Anthropic 目录用于其明确来源范围，不替代三站。

PR60 已合并人工登录返回后的自动接续、当前会话复用与完整 JD 到报告。PR61 已合并明确选择的工作区 Cookie 保存/恢复；默认不持久保存，不读取日常浏览器。Windows 使用当前用户 DPAPI，Linux/macOS 使用仅所有者权限文件且不加密。密码、localStorage、IndexedDB 不保存；Cookie 恢复不证明账号仍有效。见 [本机会话说明](SAVED_SESSION_D04.md)。

本源码提供明确列出的猎聘正常搜索与资源契约，原生模式仍须主动选择；未知业务、SSO或跨域行为不能通过开关自动获得支持。历史 robots 拒绝、页面清空、正常列表成功和首个详情已关闭分别留有记录，见[取数证据](CURRENT_ACQUISITION_EVIDENCE.md)及[首个关闭详情](NATIVE_CLOSED_DETAIL.md)。BOSS/51job没有原生契约，三站正常账号到所选完整JD仍待现场验收。

## 当前实现与实站缺口

- 猎聘单次正常密码表单、人工验证交接、自动接续、小批身份去重、查询检查点和取数结果统计已有实现；真实账号、会话和完整JD验收仍由 #50/#51/#53/#63 跟踪，不能把按钮可用当作站点认证。
- 工作区网络偏好、代理认证、有限读取重试、公开任务暂停和重启后明确继续已有实现，各范围以[交付清单](DELIVERY_STATUS.md)及原PR自身验收为准。
- 公开目录、分类及相邻页的实际采样与浏览器登录链路分别记录；四日期、91个不同完整职位URL来自独立报告，并不代表91个相关目标岗位或一次重算后的报告。

至少30个不同目标岗位、实际日期、全部尝试分母、身份/全文人工审核及阈值核准仍须整体核验；已有日期覆盖不替代剩余条件。无 href、特殊 iframe/SSO/业务响应需专用契约，正常登录、过期与结构变化恢复仍待实站证据。PAC、VM宿主机及VPN/TUN产品现场矩阵、长期调度和跨设备账户配额继续分别跟踪；已有受控验证不认证用户当前环境。

## 数据与回退

本矩阵是附加元数据，不改 jobs.sqlite 的 schema 1、原文、已有报告、个人证据、配额和会话格式，不自动迁移历史报告。升级/回退实测与操作见 [工作区兼容验证](WORKSPACE_COMPATIBILITY.md)。不同旧版本对 `browser_fetch` 等来源方式的支持不能一概而论，必须使用对应源码和完整备份核验。

[按钮指南](GUIDED_COLLECTION.md) · [访问契约](NATIVE_BROWSER_D02.md) · [网络状态](NETWORK_COMPATIBILITY_STATUS.md)

按#166增加明确选择的公开分类相邻页：只沿保存的同分类编号链接，核对标题、canonical与当前页号，每批最多5个未在前页出现的职位，最多前5页；原名单和报告保留。该范围接续#168共享持久限额，合成测试与本次源码/程序资格分开记录。旧PR167的现场观察属于历史证据，不代表当前组合重新取得或认证真实页面。自定义关键词、地区与真实账号验收仍独立跟踪。
