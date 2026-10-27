# 文档目录

新用户先读[从零开始](FIRST_RUN.md)，了解现版本能力先读[当前交付与待验收](STATUS.md)。本目录把日常指南与历史记录分开；无需按文件名逐个阅读。

## 按目的查找

| 你要做什么 | 从这里开始 |
| --- | --- |
| 下载安装、第一次运行 | [用户指南目录](guides/README.md) · [首次启动](FIRST_RUN.md) |
| 从 JD 到原报告、本人证据 | [操作手册](WORKFLOWS.md) · [质量复核](QUALITY_REVIEW.md) |
| 配置采集浏览器、代理、DNS、PAC | [网络与浏览器目录](network/README.md) |
| 服务器运行、浏览器访问 | [Docker 单人工作台](DOCKER.md) |
| 理解模块职责、开发和发版 | [开发文档目录](development/README.md) · [架构](ARCHITECTURE.md) |
| 判断已交付与未验收 | [当前状态](STATUS.md) · [能力矩阵](ACQUISITION_CAPABILITIES.md) |
| 查看版本变化 | [发行说明目录](releases/README.md) |
| 找过去的调查、PR 和验收原文 | [历史归档目录](archive/README.md) |

## 使用和部署的主要入口

- 桌面：[Windows](WINDOWS_PORTABLE.md)、[Linux / macOS](POSIX_DISTRIBUTION.md)。
- 自有运行环境：[Python 高级安装](PYTHON_DISTRIBUTION.md)、[Docker](DOCKER.md)。Python 包无需与桌面包同时安装。
- 取数：[公开分类](LIEPIN_PUBLIC_CATEGORY.md)、[固定公开来源](PUBLIC_SOURCES.md)、[浏览器向导](GUIDED_COLLECTION.md)、[高级表单案例](COLLECTION_FORM_CASES.md)。
- 数据：[个人证据契约](EVIDENCE_CONTRACT.md)、[工作区兼容](WORKSPACE_COMPATIBILITY.md)、[更新与恢复](UPDATES.md)。

## 阅读历史记录的规则

发行说明保留版本边界；日期审计、旧 PR 和候选记录保留原提交、统计、失败及当时的“待验收”结论。最新状态通过 STATUS、能力矩阵和总控 Issue 查证，不从某段历史叙述推断。

`DELIVERY_STATUS.md` 保留为旧链接及预算检查的稳定入口，其阶段日志全文已归档。其他当前文档继续保留原路径，避免打断安装包生成、源码注释和已有使用入口；专题目录提供按用途的导航。许可证和机器证据保持原文。

## 后续如何维护

1. README 只保留产品目的、当前安装方式、首份结果、限制和文档入口，不再逐版本追加长公告。
2. 新版本发布后更新 README、FIRST_RUN、STATUS 及部署示例；发布事实必须有正式 Tag 和验收回执支持。
3. 当前版发行说明保留 `docs/RELEASE_<版本>.md`。进入下一正式版本时，将旧版移入 `releases/`，修正入链并更新目录。
4. 日期审计和一次性实施记录放入 `archive/` 的对应类别，注明时间与提交；现用操作契约留在当前指南中。
5. 更新 FIRST_RUN 后重新生成 `START_HERE.html`；能力矩阵按原生成器维护。仅整理文档时核对链接、生成文件和相关文档契约，不为此重跑产品全矩阵。
