# Vibe Job Radar｜招聘要求与个人证据工作台

把真实岗位 JD 整理成可追溯的要求，再结合本人项目、附件和实际指标，形成能力描述与待补证清单。默认研究方向为垂直领域算法、时间序列算法和架构师。

本页对应版本：**0.20.3**；正式发行以 [Releases](https://github.com/saksim/vibe_job_radar/releases) 为准。查看[本版变化](docs/RELEASE_0_20_3.md)、[当前交付与限制](docs/STATUS.md)或[完整文档目录](docs/README.md)。

## 选择适合你的启动方式

| 你要怎么用 | 下载或指南 | 需要准备什么 |
| --- | --- | --- |
| Windows 桌面 | [Windows 正式包](https://github.com/saksim/vibe_job_radar/releases/tag/v0.20.3) · [启动说明](docs/WINDOWS_PORTABLE.md) | Windows x64；包内带 Python 和采集浏览器 |
| Linux / macOS 桌面 | [桌面正式包](https://github.com/saksim/vibe_job_radar/releases/tag/desktop-v0.20.3) · [启动说明](docs/POSIX_DISTRIBUTION.md) | Linux x64 或 macOS arm64；系统库与签名限制见说明 |
| 服务器运行，浏览器访问 | [Docker 部署](docs/DOCKER.md) | 一个拥有者、一个持久工作区；远程访问需 HTTPS |
| 自行管理 Python 环境或使用 CLI | [Python 高级安装](docs/PYTHON_DISTRIBUTION.md) | Python 3.10+；按所用能力安装可选依赖 |

**普通桌面用户不需要另装 Python 包。** Python 包是同一产品的另一种安装形式，供开发、自动化和自行维护运行环境使用，并没有额外的一套业务功能。三个 Release 名称对应安装包类型，版本号和源码一致。

桌面包仍为 unsigned / unnotarized，具体系统限制见对应安装说明。Docker 镜像提供单人服务器工作台，支持导入、公开来源、分析、证据和报告；不包含招聘平台登录浏览器，也不接管访问者电脑上的 Chrome、Edge 或 Cookie。

第一次使用，按[从零开始](docs/FIRST_RUN.md)操作；源码目录也提供可离线打开的 [START_HERE.html](START_HERE.html)。

## 完成第一份岗位研究

1. **取得真实完整 JD。** 已有获准处理的材料可粘贴或导入，也可明确选择公开来源或采集入口。用“运行合成演示”熟悉界面时，演示结果始终标为合成数据。
2. **生成并核对原报告。** 检查岗位正文、逐条原文引用、纳入数量和排除原因。只有摘要、岗位不匹配或没有正向 AI 编程要求时，结果会说明限制。
3. **加入本人的证据。** 点击“用我的证据继续”，在同一报告中填写实际项目、负责范围、附件和可核对指标，再生成个人报告。
4. **保留缺口。** 没有证据的数字保持待填；样本频次不是市场占比，映射覆盖率不是胜任度或录用概率。

完整步骤见[操作手册](docs/WORKFLOWS.md)与[证据及报告规则](docs/EVIDENCE_CONTRACT.md)。

## 数据获取有哪些选择

| 入口 | 适用范围 |
| --- | --- |
| 粘贴 / 导入 JD | 处理自己已有权使用的完整材料；无需搜索 Key |
| [猎聘公开分类](docs/LIEPIN_PUBLIC_CATEGORY.md) | 明确选择架构师或算法工程师固定分类，每批 1～5 项，可分别确认下一批或相邻页；不执行自定义关键词或地区检索 |
| [固定公开目录](docs/PUBLIC_SOURCES.md) | Anthropic、Cloudflare、Cursor 的指定公开目录；关键词与地区在本机筛选，不能替代全市场搜索 |
| [浏览器采集向导](docs/GUIDED_COLLECTION.md) | 按平台、关键词和实际访问范围正常操作；登录、资源及页面兼容仍需逐站验收 |
| [高级 URL / Search / JSON](docs/COLLECTION_FORM_CASES.md) | 已有 URL、自己的 Brave Key 或获授权的数据接口；不共享向导中的浏览器会话 |

**BOSS、猎聘、51job 的正常账号到完整 JD 和报告链路尚未全部通过实站认证。** 某次取得卡片、单个正文或受控测试通过，都不代表三站自动取数已经完成。当前能力见[能力矩阵](docs/ACQUISITION_CAPABILITIES.md)，逐次真实观察见[取数证据](docs/CURRENT_ACQUISITION_EVIDENCE.md)。

来源许可、robots、请求频次、共享额度和冷却继续生效；失败不会退还已消耗额度。验证码、访问拒绝、网络或页面结构变化会明确停止，不编造正文或采集结果。

## 后台执行、数据和更新

已接受的有限采集批次与报告任务由后端执行。关闭或刷新工作台网页后可重新打开查看；**应用或服务器进程仍须运行**。暂停、退出及不确定中断保留原状态，进程重启后不会自动重放请求。固定公开目录另有明确启用的[每日计划](docs/PUBLIC_DAILY_SCHEDULE.md)和[独立进程](docs/PUBLIC_WORKER.md)。

桌面工作区默认保存在用户主目录的 `.vibe-job-radar`；Docker 工作区保存在服务器持久卷。升级前停止任务和程序，备份整个工作区，保留原报告与旧版本。详见[版本与升级](docs/UPDATES.md)、[工作区兼容及回退](docs/WORKSPACE_COMPATIBILITY.md)。

[团队设备服务](docs/TEAM_SERVICE.md)提供可选的设备权限、共享配额和公开来源同步；它与单人服务器工作台是不同的服务边界，不等于多人私人工作区已经交付。

## 从源码启动

需要 Python 3.10+。在完整解压后的源码目录执行：

```powershell
# Windows，也可双击 start_windows.bat
py -3 scripts/start_workbench.py
```

```sh
# Linux / macOS
python3 scripts/start_workbench.py
```

保持终端运行，打开终端给出的完整本机地址。已有 Anaconda 环境可以继续使用；只刷新旧网页不会更新正在运行的代码。浏览器组件、离线导入和故障处理见[首次启动指南](docs/FIRST_RUN.md)与[浏览器安装诊断](docs/BROWSER_SETUP.md)。

## 深入阅读

- [文档总目录](docs/README.md)：按安装、使用、网络、开发和历史查找。
- [当前交付与待验收](docs/STATUS.md)：已发布内容、未完成范围及需要的环境材料。
- [架构与模块边界](docs/ARCHITECTURE.md)：页面、应用服务、任务和数据的职责。
- [开发与发版晋升](docs/BRANCH_PROMOTION.md)：功能进入 dev，再经 release 晋升 main。
- [维护者源码验收](docs/SOURCE_QUALIFICATION.md)：按改动范围验证，原始失败如实保留。
- [历次发行说明](docs/releases/README.md) · [历史审计与实施记录](docs/archive/README.md)。

历史文档保留当时的提交、测试和限制；其中“当前”“本分支”“待合并”只属于记录时点，不作为现版本下载或操作入口。
