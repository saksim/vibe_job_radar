# Vibe Job Radar｜招聘要求与个人证据工作台

## 先完成一次目标岗位研究

这个项目要解决的是：**目标岗位真实 JD → 逐条 AI 编程要求 → 通用能力与分岗位补充 → 本人证据 → 可量化描述与缺口**。默认研究方向为垂直领域算法、时间序列算法和架构师；不是通用爬虫或自动生成虚构履历的工具。

打开本地工作台后，先确定方向、平台和检索词，进入平台采集。原生登录、确认访问范围并取得正文后，从结果直接查看**本批研究结论**；再点**用我的证据继续**，同一份报告会自动带入原文复核和个人证据中心，不必重新选择报告或手写 JSON。已有获准处理的真实 JD，也可从首页粘贴/导入后分析。

**没有目标岗位完整正文就不算研究完成。** 正文零纳入、岗位不匹配、只有摘要或尚无已接收 AI 编程证据时，结果说明原因，不用固定案例、普通算法技能或旧报告冒充成功。单个岗位的要求不概括成跨岗位通用底座；待补证不代表本人没有能力。

### 数据获取范围必须明确

BOSS、猎聘、51job 的浏览器采集仍需逐站现场验收，不能因入口存在就宣称三站已可用。手工导入只作为已有材料入口和正常兜底，不替代目标平台自动取数验收。

当前主干基线与待合并实现分别列在[能力矩阵](docs/ACQUISITION_CAPABILITIES.md)。同一能力快照进入采集页、适配注册表和新报告；受控测试、正常账号实测与默认启用分别记录。升级/回退须核对[已验证的工作区范围](docs/WORKSPACE_COMPATIBILITY.md)。

首页另有已接入的 **Anthropic Greenhouse** 公开目录与固定真实案例，免 Key、无需产品服务器。它可用于试用“真实来源→分析→证据”流程，但不是全市场搜索，不能替代中国招聘平台或时间序列/电力算法岗位研究。关键词、地区在本机筛选。

### 本地优先与网络支持

登录态、简历、本人证据和私人报告默认留本地，生产 Linux 部署后置。已合并的匿名本机 HTTP/SOCKS5 静态策略和明确同意的 198.18/15 加密解析保留；本分支新增的[显式本机代理认证](docs/LOCAL_PROXY_AUTH.md)由#75跟踪。高级批次接线、PAC及其他网络缺口见 [网络状态](docs/NETWORK_COMPATIBILITY_STATUS.md)。不要关闭 TLS 校验或删除配额库来制造成功。

当前为 Python 3.10+ 源码版本，尚非包含 Python/Chromium 的免安装发行版。Windows 可双击 `start_windows.bat`，其他系统使用原启动入口。新增的 `research_brief.md` 便于带走本批结论；底层完整原文、复核队列和历史报告保留。

详细的主线范围与验收见 [首份岗位研究流程](docs/MAINLINE_WORKFLOW.md)。所有后续源码迭代从实际已合并的 main 开始，不把未合并分支写成主干功能。

## 已经打开过工作台

先停止旧终端并备份工作区，完整解压新版本源码，进入新目录，沿用已成功的解释器启动。例如：

```bash
/d/code_environment/anaconda_all_css/py312/python.exe scripts/start_workbench.py
```

不需要更换 Anaconda 环境。打开本次终端给出的完整地址，从首页进入 `/guided` 新手向导。只刷新旧网页不会更新运行中的代码。完整更新与首次安装说明见 [FIRST_RUN.md](docs/FIRST_RUN.md)，或双击源码中的 [START_HERE.html](START_HERE.html)。

## 想让程序自己找岗位

在新手向导中先安装可选采集浏览器，再选择平台、关键词、1页/5条和实际访问范围。点击“打开搜索并读取岗位”。需要登录时，操作程序弹出的独立采集浏览器，在平台原生页正常登录，然后回向导读取当前列表。勾选岗位，点击“采集所选并生成报告”。

程序负责汇总列表观察到的链接、打开详情、保存最终地址和独立正文、生成本批报告。你不必填写 API endpoint、分页游标、选择器，也不必逐条复制详情 URL。没有稳定链接或结构不兼容的页面需要逐站维护，不会编造结果。

**猎聘新增一次正常密码表单提交，当前通过受控测试，尚未完成真实账号验收。** 第 3 步可在本机明确授权填写并提交一次，账号密码不会写入任务、报告或会话文件；协议、验证码和短信验证由你在平台页面完成，原查询或所选完整岗位可读后自动接续。其他平台继续使用人工登录。实站搜索访问仍有阻塞，详见 [本轮实现与实站结果](docs/LIEPIN_PASSWORD_LOGIN.md)。默认退出服务不保存 Cookie；第 2 步明确勾选“在此工作区本机保留”后可尝试恢复本站 Cookie，再通过正常页面继续取数。Windows 使用当前用户 DPAPI，Linux/macOS 使用仅所有者权限且不加密；不保存密码、localStorage 或 IndexedDB，不保证账号仍有效。使用与清除方法见 [本机会话恢复](docs/SAVED_SESSION_D04.md)。三站均为 `not_live_verified`，没有真实账号与样本验收不能宣称实站成功。

## 不需要浏览器采集，也能使用

本地粘贴、导入和规则分析不需要任何 Key 或额外运行依赖。先点击“运行合成演示”认识报告，再粘贴你有权处理的真实完整 JD，保存并分析。下载 `requirements_zh.csv` 核对逐条原文。

原 `/advanced` 保留 URL、Brave Search API 和授权 JSON 数据源路线。已有链接走 URL；想用搜索服务找链接，需要自己的 Brave Key；没有数据供应方就不选 JSON 接口。见 [逐格填写案例](docs/COLLECTION_FORM_CASES.md)。旧 HTTP 路线不共享新向导的浏览器会话。

高级页同时提供原文复核、个人项目、附件和指标、精确要求映射、个人报告与证据包。没有本人证据的数字保持待填，能力覆盖率不是胜任度或录用概率。

## 强制频次与运行边界

新向导默认页面导航至少15秒、60次/小时、200次/滚动24小时；经桥接的HTTP请求至少0.5秒、600次/小时、3000次/滚动24小时。同一工作区和站点的任务共享持久化配额，失败不退回，限流触发冷却，界面不能提高上限。

新向导的当前批次由后端线程执行，关工作台网页仍继续；暂停/停止控制任务，关闭终端退出服务。旧高级页仍由网页驱动后续步骤。这不是开机自启的长期调度服务。

浏览器请求保留公网DNS/IP绑定、TLS和域名边界。已接入的公开查询和浏览器路径在用户阅读隐私说明并明确同意后，可修复系统解析全部落入 198.18/15 的情况；解析商可看到域名和出口。其他私网/混合回答仍拒绝，不关闭安全检查。验证码、robots拒绝、资源域名或页面结构变化仍可能中止采集。

## 使用说明与开发契约

| 目标 | 文档 |
|---|---|
| 安装、更新、启动和第一份结果 | [零基础指南](docs/FIRST_RUN.md) |
| 不整理URL，按按钮找岗位 | [浏览器采集向导](docs/GUIDED_COLLECTION.md) |
| 原有URL/Search/JSON表单 | [字段来源和填写案例](docs/COLLECTION_FORM_CASES.md) |
| 采集和本人证据操作 | [操作手册](docs/WORKFLOWS.md) |
| 已实现和未认证的边界 | [采集能力](docs/ACQUISITION_CAPABILITIES.md) |
| Pythonic 插件与接口 | [插件契约](docs/BROWSER_PLUGIN_CONTRACT.md) |
| 升级和源码合并说明 | [0.2.0 说明](docs/RELEASE_0_2_0.md) |

## 启动与验收

Windows可双击 `start_windows.bat` 或执行 `py -3 scripts/start_workbench.py`；macOS/Linux执行 `python3 scripts/start_workbench.py`。Python需3.10+。默认数据保存在用户主目录 `.vibe-job-radar`，停止服务后备份整个目录，升级时不要清空。

```bash
python scripts/run_tests.py --report acceptance/results.json
python scripts/start_workbench.py --doctor
python scripts/run_demo.py
# 已安装可选浏览器依赖后，运行受控双浏览器测试
python scripts/run_guided_browser.py
```

安装CLI是可选项：`python -m pip install -e .`。Playwright只在启用浏览器功能时需要，可在向导里安装。自动测试使用人工职位与模拟上游，不能替代真实招聘网站认证；以对应提交的CI为准。

旧CLI文档完整保留在 [README_CLI.md](README_CLI.md)。服务仅监听本机，不适用于公网多人部署。升级新增的 `browser_fetch` 来源不保证旧版本认识，回退时恢复对应的完整工作区备份。

### 已装Playwright仍提示浏览器未就绪？

先读[浏览器安装与启动修复](docs/BROWSER_SETUP.md)。`pip install Chromium`装的是同名Python包；浏览器本体要通过当前Python的`-m playwright install chromium`下载。新手页可点“检查浏览器（不采集）”，区分包、可执行文件与真实空白页启动；安装失败阶段和脱敏输出直接可见。


### 维护者：源码绑定验收

构建候选前运行 `python scripts/verify_candidate.py`，再运行 `python scripts/build_candidate.py`。只有与当前源码一致且四项本地检查全部通过的证据才能构建；这不是普通用户查询岗位的前置步骤，也不证明实际 VPN/招聘站点认证。详见 [源码验收说明](docs/SOURCE_QUALIFICATION.md) 与 [当前交付清单](docs/DELIVERY_STATUS.md)。

真实取数验收可先登记范围，再用[本机观测账本](docs/LIVE_ACCEPTANCE_LEDGER.md)离线核对原任务、原报告和入库记录。工具保留失败与未完整项，不能代替现场30条/3日期、完整尝试分母、用户审核或自动认证。

### 原生浏览器实验（D02 / #48）

新建任务可显式选择原生实验并确认范围，旧HTTP桥仍默认。页面HTTP/TLS由浏览器完成，经过只透明转发加密字节的本机公网目标CONNECT守卫；不是Python重发页面。当前仅猎聘主站bootstrap契约，真实业务API/跨域依赖尚待逐站核实，BOSS/51job未启用native；不能把开关出现当成三站取数成功。详见[实现与限制](docs/NATIVE_BROWSER_D02.md)。
