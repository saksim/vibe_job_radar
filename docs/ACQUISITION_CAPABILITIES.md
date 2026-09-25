# 当前取数能力与验证范围

核对日期 2026-09-24，已合并主干基线 `ec458799`（PR61）。下表记录浏览器通道，由 `acquisition_status.py` 生成，同一份数据进入适配注册表、采集页与新报告的 `software_acquisition_capabilities`。修改生成数据后执行 `python scripts/check_acquisition_status.py --write` 并核对证据；自动测试检查文字表格未与程序脱节。当前候选新增的公开HTTP采集范围见表后说明，不改变浏览器实站认证状态。

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

当前[52项候选](CURRENT_DELIVERY_CANDIDATE.md)提供明确选择的猎聘公开HTTP路径：所给详情/分享链接，以及发布方架构师、算法工程师两个职业分类，支持保存名单续取、已观察相邻页、等待恢复和当前小批后台完成。已取得89份来自不同职位URL的完整正文，其中49个符合原三类角色规则，实际采集日期为2026-09-24。完整性、身份、原失败及来源证据分别保留；原文和私有报告不上传。范围见[公开分类](LIEPIN_PUBLIC_CATEGORY.md)、[共享配额](ADVANCED_SHARED_RATE.md)、[等待恢复](PUBLIC_CATEGORY_RECOVERY.md)和[后台执行](CATEGORY_BACKGROUND.md)。

本分支按#166增加公开分类的相邻页选择：只沿已保存分页区明确给出的连续链接，每次确认仍最多5条，跨页去重并保留本页未选名单，最多前5页。当前提交的真实采集、浏览器与实际Windows程序验收分别记在Issue/PR；架构师和算法工程师的第二页分别已有实际读取记录；第3～5页仍仅有受控结构测试，不等于实站认证。原无分页证据的检查点不能推断下一页。

PR187另修复共享robots前导通配符。BOSS公开首页一次200观察有107个实际职位链接，但同一算法详情被原跳转规则判为登录/验证入口，0完整正文；BOSS原生访问契约仍不可用。51job的HTML robots仍不可用。该观察不改变上表的专用适配和实站认证状态。

这个公开路径不需要账号，不执行自定义关键词/地区检索。它不等于表中浏览器搜索、正常登录、重启会话和过期恢复已验通；这些状态仍受#50/#54/#63约束，不能用公开正文数量提升浏览器认证或启用默认切换。

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

三站浏览器正常登录、原关键词列表到小批完整JD、跨日期至少30条/3日期、过期与结构变化现场恢复仍须逐项验收。猎聘公开HTTP已有完整正文，不补足这些浏览器与跨日期缺口。无href、特殊iframe/SSO/业务响应需专用契约。PAC、代理认证、VM宿主机等已有待审有限实现，其全路径和实际VPN/TUN现场、长期运行和跨设备账户配额仍未完成。完整清单见 [交付状态](DELIVERY_STATUS.md)。

## 数据与回退

本矩阵是附加元数据，不改 jobs.sqlite 的 schema 1、原文、已有报告、个人证据、配额和会话格式，不自动迁移历史报告。升级/回退实测与操作见 [工作区兼容验证](WORKSPACE_COMPATIBILITY.md)。不同旧版本对 `browser_fetch` 等来源方式的支持不能一概而论，必须使用对应源码和完整备份核验。

[按钮指南](GUIDED_COLLECTION.md) · [访问契约](NATIVE_BROWSER_D02.md) · [网络状态](NETWORK_COMPATIBILITY_STATUS.md)
