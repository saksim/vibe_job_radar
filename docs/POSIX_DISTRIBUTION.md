# Linux / macOS 完整程序包

0.12.0 程序包含[系统密钥库组件](SYSTEM_CREDENTIALS.md)，可加密保存已有本机代理凭据，并明确迁移团队设备凭据。Linux 仍需现有 Secret Service 默认密钥库，macOS 需默认钥匙串授权；缺失或锁定时停止新请求，不自动创建库或退回明文。

从 [desktop-vVERSION](https://github.com/saksim/vibe_job_radar/releases/tag/desktop-vVERSION) 选择与系统一致的 tar.gz。Linux x64 在 Ubuntu 22.04 x64 构建，macOS arm64 在 macOS 15 arm64 构建；其他发行版、系统版本、架构和个人干净设备尚未认证。需保留完整目录、符号链接和可执行权限。

程序内含 Python、Playwright、Chromium、PAC 和 NTLMv2 组件，不需要另装 Python。Linux 仍依赖系统浏览器库；缺少库时按诊断处理，或选择系统上已安装的兼容浏览器。macOS 包没有发布者签名或公证；遇到系统阻止时保留诊断，不能把 CI 成功当成 Gatekeeper 已验收，不建议绕过系统安全设置。

## 下载与运行

1. 下载对应架构的 tar.gz、该平台 manifest 和 verification JSON，以及 SHA256SUMS.txt。用 Linux 的 sha256sum 或 macOS 的 shasum -a 256 核对下载文件与校验表；校验表与程序应来自同一正式 Release。
2. 使用系统 tar -xzf 解压到新的可写目录；不要用可能丢失链接或权限的跨平台压缩工具。完整目录内运行 ./VibeJobRadar；需要原工作区时传入 --workspace /绝对路径。保留终端，按 Ctrl+C 关闭。自动打开浏览器不可用时，在本机打开终端显示的完整地址，不分享其中令牌。
3. 先完成离线诊断 --doctor，再在界面检查捆绑浏览器；遇到依赖或浏览器故障时按具体提示处理。尚无可用桌面的 Linux 可用 --no-browser 启动并在同机浏览器打开。工作台仍只监听本机。

## 升级和回退

停止任务、计划与程序，备份整个工作区，再把完整新包解压到另一目录，用同一个 --workspace 路径重新打开。保留旧包与备份；降级前遵循工作区版本的明确回退规则，不直接让旧程序改写新格式。新包复用实际通过测试的归档，不在发版时重新打包。

此包使用完整包手动更换。Windows 的自动替换、DPAPI 凭据保存和 Windows 登录启动仍由原 Windows 正式版提供，未声称移植到 Linux/macOS。当前平台凭据操作以实际支持提示为准，不以明文存储替代系统加密。既有 Python wheel/sdist 继续用于已有 Python 的环境。

## 发行证据与边界

每个平台的实际解压程序在没有 Python 的 PATH、不同当前目录和带中文/空格工作区中验证：诊断、捆绑 Chromium、原生请求守卫与清理、冻结 NTLMv2、URL/域名 PAC 工作者、源码与冻结程序的系统凭据保存/迁移/锁定拒绝、人工完整 JD 导入与原报告、独立进程重开、运行前后包内容一致。人工夹具不属于真实招聘数据，不代表真实站点或企业 VPN 验收。完整主干 CI、产物大小、SHA-256、来源提交、嵌入身份和实际运行证明均通过后才发布。

Linux/macOS 配套正式 Tag 为 desktop-vVERSION；Windows 仍使用 vVERSION 并保持 Latest，Python 包使用 python-vVERSION。生产服务主机/域名、企业代理、签名/公证、个人干净设备和人工材料继续单列。

[Issue #399](https://github.com/saksim/vibe_job_radar/issues/399) · [总控 #57](https://github.com/saksim/vibe_job_radar/issues/57)

维护者发布顺序为 Windows → Python → Linux/macOS。三个发行工作流共用串行发布组，保留既有下载链接；如果前置发行尚未完成，后续流程会停止并保留结果。

维护者本机构建仍可使用 build_posix_portable.py；密钥库验收需在隔离桌面会话中明确加 --isolated-vault。Linux 先提供 gnome-keyring/dbus 并使用 dbus-run-session 包裹命令；macOS 临时切换默认钥匙串，finally 恢复。该选项只用于人工测试库，不解锁个人库。正式 CI 自动使用一次性运行器，不新增全量单测矩阵。
