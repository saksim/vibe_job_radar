# Windows x64 便携候选包

关联 #91 / #30 H6 / #3 / #57。本分支增加包含 Python、项目资源、Playwright、配套 Chromium 和 truststore 的独立候选包；正式签名发行、自动升级及其他系统仍未完成。构建成功不能替代三站实站登录与完整 JD 验收。

## 使用已验证的候选

在对应提交的 `windows-portable-candidate` 工作流中核对 `build-and-run-executable` 成功，下载 `windows-x64-portable-candidate` 及 `windows-portable-evidence`。这是保留 7 天的 CI 候选产物，尚非 GitHub Release；失败运行不会上传新的候选 ZIP。

1. 按 evidence 中 `manifest.json` 的文件名和 SHA-256 核对 ZIP，可用 PowerShell `Get-FileHash -Algorithm SHA256 "候选文件路径.zip"`。摘要只核对文件一致性，不是发行签名。
2. 将里面的 `VibeJobRadar` 整个目录解压到新目录，再双击 `VibeJobRadar.exe`。保留 `_internal` 及其他随包文件，不在 ZIP 内运行，也不只复制 exe。无需安装 Python 或改动原 Anaconda 环境。
3. 浏览器打开本机工作台；默认仍使用用户目录 `.vibe-job-radar`。从向导点击“检查浏览器（不采集）”，真实空白页检查通过后再创建任务。也可明确选择、检查已经安装的 Edge；不会自动安装 Edge 或接管日常浏览器。
4. 组件缺失或需更新时，停止任务和每日计划、关闭所有同工作区服务并备份整个工作区，再完整解压已验证的新候选到另一个目录。候选不会自己执行 pip、覆盖正在运行的组件或自动更新。

命令行支持原有 `--workspace "D:\岗位研究"`、`--doctor`、`--no-browser` 等工作台参数。工作区属于用户，不放入程序包；多实例互斥仅适用于具备任务所有权修复的版本，旧源码不能与候选同时操作同一工作区。

升级与回退沿用[工作区兼容说明](WORKSPACE_COMPATIBILITY.md)以及[网络偏好 v2 回退要求](WORKSPACE_PROXY_SETTINGS.md)。从便携包切回源码时仍需满足源码自己的环境依赖；更换启动形式不会转换或清空原数据。关闭程序期间每日计划不运行，不安装开机自启或常驻服务。

## 维护者构建与证据

工作流使用独立 Windows x64 runner 上的官方 CPython 3.12。固定 PyInstaller 6.22.3、Playwright 1.63.0、packaging 26.0、truststore 0.10.4；其余实际构建环境包版本写入 `PORTABLE.json`，不宣称全部传递依赖锁定或字节可重现。Python、项目及随包主要依赖许可证放入 `licenses/`，Chromium/驱动原目录的许可文件保留。

先设置 `PLAYWRIGHT_BROWSERS_PATH=0` 并通过该 Playwright 安装 Chromium，再运行 `python scripts/verify_candidate.py`、`python scripts/build_windows_portable.py`。打包器不负责安装构建环境。依据为 [Playwright 官方打包说明](https://playwright.dev/python/docs/library#pyinstaller)和 [PyInstaller 冻结运行时说明](https://pyinstaller.org/en/stable/runtime-information.html)。

构建门槛与验收：

- 当前源码四步验证全部通过，单测失败、错误、跳过均为零；源码逐文件指纹在构建前后相同。全量单测等待上限为 600 秒，其余单项保持 180 秒。此调整对应已超过三分钟的 Windows 全量回归，不跳过或减少测试。
- 实际冻结 exe，从另一个当前目录启动，在含中文和空格的临时工作区运行；子进程移除 Python 路径和相关环境变量。runner 自身仍有 Python，因此证据不宣称已覆盖完全未安装 Python 的个人电脑或所有 Windows 版本。
- 实际 exe 完成 SQLite/诊断、所有页面资源、不可变组件拒绝安装、配套 Chromium 真正启动关闭；浏览器必须来自包内，不能借用 runner 缓存或 Edge 冒充。
- 人工 JD 进入原分析链路，产生原报告和 CSV；真实配套浏览器验证 390px 页面及便携说明，重新启动 exe 仍能打开报告、计划默认关闭且重新检查浏览器。无招聘站点请求，不安装系统 CA。
- 验收前后逐文件检查包未自行变化，验收清单与最终入 ZIP 文件逐一绑定，输出 ZIP 摘要。失败覆盖本次 manifest 为失败，运行阶段与错误类型留在 `portable-verification.json`；旧摘要命名的候选不删除，旧成功证据不能代表本次构建。

结构化验收记录不写本机访问令牌、原始进程日志或用户工作区。构建只使用仓库代码、干净构建环境及公开组件，登录 Cookie、个人浏览器资料和实际报告不会作为输入。候选不包含自动升级器、发布凭据、服务部署或招聘网站认证结论。

## 当前限制

候选未签名，尚未覆盖个人设备的全部系统策略和安全软件。它解决运行环境准备与正确诊断，既不改变正常认证流程，也不放宽 robots、访问节奏、网络或站点约束。三站现有卡点继续在 #49/#50/#54/#55/#56/#63 跟踪；正式发行与自动升级仍须独立验收。当前 head 的实际构建结果以对应 PR / CI 记录为准，源码测试通过不等于 exe 验收完成。
