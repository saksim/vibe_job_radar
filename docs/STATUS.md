# 当前交付与待验收

本页按 **2026-10-26 已正式发布的 0.20.2** 整理。发行提交为 `870eabb2160c63db49bbafd4d0f53f575fdd3db8`；后续变化以[正式 Releases](https://github.com/saksim/vibe_job_radar/releases)和[产品目标 #3](https://github.com/saksim/vibe_job_radar/issues/3)为准。下方保留这个已发布快照；新一轮 [0.20.3 变更](RELEASE_0_20_3.md)包含文档整理、两站新批次去重、原生连接关闭修复与搜索等待优化，正式完成状态以对应 Tag 和交付回执为准，不继承 0.20.2 的验收数字。

## 已发布

| 交付形式 | 正式入口 | 范围 |
| --- | --- | --- |
| Windows x64 | [v0.20.2](https://github.com/saksim/vibe_job_radar/releases/tag/v0.20.2) | 完整桌面程序，保留本机升级与浏览器组件 |
| Linux x64 / macOS arm64 | [desktop-v0.20.2](https://github.com/saksim/vibe_job_radar/releases/tag/desktop-v0.20.2) | 完整桌面程序，系统依赖及签名限制见包说明 |
| 自有 Python 环境 | [python-v0.20.2](https://github.com/saksim/vibe_job_radar/releases/tag/python-v0.20.2) | wheel / sdist；自行管理解释器和可选组件 |
| Docker amd64 / arm64 | `ghcr.io/saksim/vibe_job_radar:0.20.2` | 单拥有者服务器工作台；[部署范围](DOCKER.md) |

三个 Tag 来自同一产品版本。普通桌面用户无需另装 Python 包；Docker 也不包含招聘平台登录浏览器。

0.20.2 修复了分站关键词范围保护、51job 详情路径边界及已接收连接的关闭遗漏，并完善失败诊断。报告、个人证据、公开来源、有限后台任务、桌面、CLI、团队和原更新链路保留。具体变化见[本版说明](releases/RELEASE_0_20_2.md)，模块职责见[架构](ARCHITECTURE.md)。

## 已有证据能证明什么

发布提交的 12 类 main 工作流首次通过，七组源码各 3682 项；Windows 成品 30 项、Linux/macOS 各 10 项、Docker 两架构匿名运行各 22 项。旧 0.20.1 更新器对 19 份发行元数据的识别与摘要校验已完成。对应回执见[总控交付记录](https://github.com/saksim/vibe_job_radar/issues/57#issuecomment-6409518644)。这些数字属于该发布提交，不是任意后续分支的验收结果。

公开来源与猎聘固定分类已有真实正文及原报告观察；正常浏览器搜索也有局部实站记录。它们不能替代正常账号、相关完整 JD、分页、跨日期和人工复核的完整认证。逐站能力见[能力矩阵](ACQUISITION_CAPABILITIES.md)，原成功与失败见[实站观察账本](CURRENT_ACQUISITION_EVIDENCE.md)。

## 仍未完成

截至上述交付记录保留 18 个 Issue；以后状态变化以 Issue 本身为准。

| 范围 | 尚需完成的工作或条件 |
| --- | --- |
| [#55 BOSS](https://github.com/saksim/vibe_job_radar/issues/55)、[#56 51job](https://github.com/saksim/vibe_job_radar/issues/56) | 正常动态访问、完整 JD 与真实分页仍有专用实现及实站验收工作。先取得合法列表/分页契约和一条完整 JD；不能以入口或夹具当作完成。51job 还需明确适用访问规则。 |
| [#183](https://github.com/saksim/vibe_job_radar/issues/183)、[#263](https://github.com/saksim/vibe_job_radar/issues/263)、[#269](https://github.com/saksim/vibe_job_radar/issues/269)、[#436](https://github.com/saksim/vibe_job_radar/issues/436) | 部分独立缺陷已修复，但旧 GET、连接、存储等待和握手失败的历史物理原因尚未全部确认。工程调查继续保留原失败；这不属于用户欠交材料。 |
| #48 / #49 / #50 | 正常浏览器与会话环境、必要的本人登录验证，以及搜索→完整 JD→原报告和失效恢复的实站闭环。 |
| #54 / #315 | 获准的真实完整岗位、出处、身份与清楚分母；#54 保留 30 个不同岗位/3 日期及人工复核口径。 |
| #316 / #317 | 人工金标准标注；本人真实项目、负责边界、可公开指标及确认。不生成替代真人的标注或经历。 |
| #319 | 普通无 Python Windows 净机上的首次使用验收；开发机和 CI 不替代新手使用。 |
| #3 | 产品目标索引，继续跟踪未完成的功能、工程调查和实站验收。 |
| #210 / #320 | 历史材料保持只读；#320 原连续运行与真实重登录窗口仍暂停。 |

生产 Linux 主机、域名/HTTPS、持久工作区和备份恢复环境尚未准备；镜像验证不等于实际生产部署。Windows 签名证书、Apple 签名/公证尚未配置，桌面仍 unsigned / unnotarized。代理/VPN 产品及协议尚不明确，不假设已有可测的企业网络。

[#57 的 D11 工程范围](https://github.com/saksim/vibe_job_radar/issues/57)是能力矩阵、旧 Issue 映射、兼容与回滚证据；其已完成工程项可按证据单独收口。该阶段结束不表示上表其他 Issue 完成，产品后续工作统一由 #3 索引，#57 保留既有交付和失败记录。

## 查历史时

- [历次发行说明](releases/README.md)：每个版本具体交付的内容。
- [历史归档](archive/README.md)：冻结的 Issue 范围、日期审计、旧候选和早期验收。
- [源码资格与当前预算](SOURCE_QUALIFICATION.md)：维护者规则。
- [文档目录](README.md)：当前使用、网络和开发指南。

旧文中的“当前”“本分支”“待合并”均指当时列出的提交和日期；不能用后续成功覆盖旧失败，也不能用旧文件名推断现版本仍未交付。
