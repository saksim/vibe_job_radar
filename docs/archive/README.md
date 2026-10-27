# 历史归档

[返回文档目录](../README.md) · [当前交付与待验收](../STATUS.md) · [发行说明](../releases/README.md)

这里保存冻结的日期审计、Issue 范围、旧候选、实施过程和早期验收。原文中的提交、数量、失败与“待合并”结论均保留；相对链接按归档位置调整。查现在如何使用，请回到当前指南。

## 使用这些记录时

- 记录时点的成功或失败不自动成为现版本结论。
- 早期验收的人工材料不能替代真实JD、登录或生产环境认证。
- 原 Issue #210/#320 的正文、评论和状态没有因本次归档而修改。

## 阶段范围与开放项快照

- [ISSUE_SCOPE_0_15_0 — 0.15.0：原24项的本期范围与逐项收口](status/ISSUE_SCOPE_0_15_0.md)
- [ISSUE_SCOPE_0_15_1 — 0.15.1：剩余 17 项逐项核查](status/ISSUE_SCOPE_0_15_1.md)
- [ISSUE_SCOPE_0_15_2 — 0.15.2：剩余 18 项逐项收口判断](status/ISSUE_SCOPE_0_15_2.md)
- [ISSUE_SCOPE_0_15_3 — 0.15.3：剩余 18 项逐项收口判断](status/ISSUE_SCOPE_0_15_3.md)
- [ISSUE_SCOPE_0_15_4 — 0.15.4：剩余 18 项逐项收口判断](status/ISSUE_SCOPE_0_15_4.md)
- [ISSUE_SCOPE_0_15_5 — 0.15.5：原18个Issue逐项核查](status/ISSUE_SCOPE_0_15_5.md)
- [ISSUE_SCOPE_0_15_6 — 0.15.6：原18个Issue逐项核查](status/ISSUE_SCOPE_0_15_6.md)
- [ISSUE_SCOPE_0_15_8 — 0.15.8：原18个Issue逐项核查](status/ISSUE_SCOPE_0_15_8.md)
- [ISSUE_SCOPE_0_15_9 — 0.15.9：剩余18项的逐项判断](status/ISSUE_SCOPE_0_15_9.md)
- [ISSUE_SCOPE_0_16_0 — 0.16.0：剩余18项的逐项判断](status/ISSUE_SCOPE_0_16_0.md)
- [OPEN_ISSUES_2026_10_18 — 24 个开放 Issue 的逐项复核（2026-10-18）](status/OPEN_ISSUES_2026_10_18.md)
- [OPEN_ISSUES_2026_10_19 — 24 个开放 Issue 的逐项复核（2026-10-19）](status/OPEN_ISSUES_2026_10_19.md)
- [PENDING_ACCEPTANCE_0_15_0 — 0.15.0 待环境、材料与独立站点功能](status/PENDING_ACCEPTANCE_0_15_0.md)

## 日期审计与整合记录

- [BRANCH_AUDIT_2026_09_16 — Fork 分支与未合并提交审计（2026-09-16）](audits/BRANCH_AUDIT_2026_09_16.md)
- [CI_UTF8_REPAIR_2026_09_16 — PR #31：Windows 中文任务测试修复](audits/CI_UTF8_REPAIR_2026_09_16.md)
- [FAKE_IP_REVIEW_2026_09_16 — Fake-IP 实现复审：取消、缓存有效期与失败状态（#33 / #23）](audits/FAKE_IP_REVIEW_2026_09_16.md)
- [INTEGRATION_2026_09_23 — 当前整合范围与历史组合记录](audits/INTEGRATION_2026_09_23.md)
- [PROJECT_GAP_AUDIT_2026_09_26 — 2026-09-26 GAP审计原文](audits/PROJECT_GAP_AUDIT_2026_09_26.md)
- [PROJECT_GOAL_TRACE_2026_09_26 — 2026-09-26历史目标追溯原文](audits/PROJECT_GOAL_TRACE_2026_09_26.md)

## 旧 PR、候选与交付过程

- [历史组合候选记录：PR98（2026-09-23）](implementation/CURRENT_DELIVERY_CANDIDATE.md)
- [锁等待回归从实际commit开始计时（#174，2026-10-09）](implementation/DELIVERY_LOG_2026_10_09.md)
- [2026-09-27候选交接历史](implementation/MAIN_HANDOFF_REVIEW.md)
- [主线：首份可用的岗位研究，而不是基础设施清单](implementation/MAINLINE_WORKFLOW.md)
- [网络兼容主需求为何仍未闭环：归因与真实交付状态](implementation/NETWORK_ROOT_CAUSE_REVIEW.md)
- [2026-09-27开放PR索引历史](implementation/PR220_REVIEW_INDEX.md)
- [明确选择后的搜索到报告自动采集](implementation/PR62_AUTOMATIC_COLLECTION.md)
- [原生跨域采集：先限制辅助窗口，再执行网页](implementation/PR62_DOCUMENT_SANDBOX.md)
- [PR62：开发侧完成工程验收后，再交付用户测试](implementation/PR62_ENGINEERING_GATE.md)
- [PR62：探测不到下一页时保留当前搜索响应](implementation/PR62_SEARCH_RESULT_RETENTION.md)

## 早期架构与能力说明

- [历史文档：0.1 实施蓝图](legacy/ARCHITECTURE_LEGACY_01.md)
- [自动采集能力说明与下一阶段路线](legacy/LEGACY_HTTP_CAPABILITIES.md)

## 0.1.0 早期验收原文

报告中提及的逐例 `.log` 属于当时本地执行文件，未被 Git 跟踪；下面只列仓库保存的报告和机器可读结果。

- [Vibe Job Radar 0.1.0｜交付验收记录](acceptance/0.1.0/ACCEPTANCE_REPORT.md)
- [acceptance_results.json（原始证据）](acceptance/0.1.0/acceptance_results.json)

## 保留原路径的入口

- [非登录功能历次收口记录](../NON_LOGIN_CONVERGENCE.md)：原 0.3.0 至 0.15.0 阶段的范围与判断，现状态以 STATUS 为准。
- [早期首份公开结果说明](../FIRST_REAL_RESULT.md)：保留 Issue #20 / PR #21 的操作背景，首次安装以 FIRST_RUN 为准。
- [旧交付状态链接与源码预算](../DELIVERY_STATUS.md)：全文见上述阶段交付日志。
- [0.1.0 历史 CLI 手册](../../README_CLI.md)：保留旧命令说明，现版本安装使用 Python 高级指南。
