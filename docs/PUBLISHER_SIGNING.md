# Windows 发行者签名与 unsigned

正式 Tag 确定源码版本；SHA256 确定文件字节；Authenticode 签名把可执行文件与发行者证书绑定。这三项分别核验，正式 Release 仍可能是未签名发行。

0.14.0 增加签名接入、证明绑定和签名版升级兼容。没有配置凭据时仍正常产生未签名包，Release 名称保留 unsigned；不会生成自签名证书、安装根证书或隐藏未签名状态。实际签名服务尚需维护者完成开户、身份认证和配置。本轮没有签名凭据，因此没有本项目受信任签名发行的实测结论。

## 构建、验证和发布顺序

1. 从准确源码冻结 VibeJobRadar.exe。
2. 显式配置为 azure 或 store 时，对这一个项目入口签名并添加 SHA256 RFC3161 时间戳；任何错误停止候选构建。原第三方组件不重新署为本项目发行者。
3. 用 Windows Get-AuthenticodeSignature 要求有效的嵌入式 Authenticode 签名和时间戳，核对预先配置的发行者 Subject。签名前后不能混用摘要。
4. 将实际证书 SHA256、时间戳证书 SHA256、发行者和签名后 EXE 摘要写入 PORTABLE.json；实际程序验收再次核验签名。签名证明在内嵌记录、运行证明、manifest 和 provenance 中必须一致。
5. 保持全部原功能资格检查；正式发布复用该 main 的已验证 ZIP，不在发布步骤重新签名或重建。
6. 升级检查显示公开发行记录中的签名状态；解压后和自动启动前再次用本机 Windows 验证实际文件与证书。身份、摘要、信任或时间戳无效时停止，保留原程序、数据和已完成备份。

签名对象是 VibeJobRadar.exe；ZIP、Python 包、Linux 包没有因此获得 Windows 签名。依赖文件继续由原始完整包摘要与清单校验。核验不执行被检查文件。系统信任判断可能受当前设备根证书、撤销服务或企业策略影响，失败不自动降级为未签名执行。

## GitHub Actions：Azure Artifact Signing

现成接入方式为微软 Artifact Signing（原 Trusted Signing）。维护者先确认所在地区和个人/组织身份符合服务资格，再建立 Public Trust 证书配置；不要选私有信任配置用于公众发行。价格、地域和身份认证条件以[微软服务文档](https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart)为准。

在 Azure 给专用身份授予所选证书配置的 Artifact Signing Certificate Profile Signer 权限。配置 GitHub OIDC 联合身份，其 subject 限定为本仓库的 main 引用，audience 为 api://AzureADTokenExchange；不授予其他仓库、PR 或任意分支。不要把代码签名私钥提交到仓库。

设置仓库 Actions Variables：

| 名称 | 内容 |
|---|---|
| WINDOWS_SIGNING_MODE | azure；尚未配置时留空或 unsigned |
| WINDOWS_SIGNING_SUBJECT | 证书完整 Subject，必须与实际签名一致 |
| AZURE_SIGN_ENDPOINT | 该账号区域的 HTTPS codesigning.azure.net 端点 |
| AZURE_SIGN_ACCOUNT | 签名账号名 |
| AZURE_SIGN_PROFILE | Public Trust 证书配置名 |
| AZURE_SIGN_CLIENT_ID | 专用联合身份应用 ID |
| AZURE_SIGN_TENANT_ID | 租户 ID |
| AZURE_SIGN_SUBSCRIPTION_ID | 订阅 ID |

仅 main 的 push 构建启用配置；PR、其他分支和手动 dispatch 保持 unsigned。云登录使用 azure/login@v2 和 OIDC，不需要仓库保存长期 client secret。固定安装 ArtifactSigning PowerShell 模块 0.1.20；调用时只使用 Azure CLI 凭据，禁用交互登录及其他自动凭据选择，不缓存签名组件。错误配置必须修正后重新产生候选，不能降级发布。

首次配置后用下一个实际版本的完整 main 构建完成“签名→运行资格→发布”的验收，确认发行名称 signed、provenance.signed=true、证明绑定及设备上的签名有效。未完成此实际验证前，不把接入代码称作签名服务已认证。

## 本机构建：证书库或 HSM/KSP

本机显式构建另支持 Windows 当前用户 My 证书库；适用于已经安装的受信任证书及其硬件/云 KSP。先按服务商文档配置证书访问，再设置这些进程环境变量运行原构建器：

| 名称 | 内容 |
|---|---|
| VJR_WINDOWS_SIGNING_MODE | store |
| VJR_SIGNTOOL | Windows SDK signtool.exe 的绝对路径 |
| VJR_SIGNING_THUMBPRINT | 证书的 40 位十六进制 SHA1 选择指纹 |
| VJR_SIGNING_SUBJECT | 预期证书完整 Subject |
| VJR_SIGNING_TIMESTAMP_URL | 服务商规定的 RFC3161 时间戳 URL |

选择证书所用 SHA1 指纹不等于签名使用 SHA1：文件和时间戳都固定为 SHA256。构建器不导入 PFX、不传密码、不配置硬件驱动、不自动选择任意证书。没有适合自动化的现有环境时，先完成服务商接入；本机候选也不能绕过准确 main CI 的正式发布条件。

## 旧版本过渡与界面

0.13.0 及以前的 Windows 升级器只接受 signed=false。先升级到这次未签名的 0.14.0，可获得未来签名版的兼容核验入口；若直接跨过过渡版，应手动下载、核对并启动完整新版包。不要修改既有 Tag 或把旧包伪标成 signed。

检查后的界面区分“发行记录含签名，下载后在本机复核”和“本机签名核验通过”。未签名包继续显示中文说明和 unsigned，摘要不能代替发行者身份。

## 仍可能出现的系统提示

有效签名可显示发行者身份，但不能保证新文件立即获得 SmartScreen 信誉，也不保证消除所有企业策略或杀毒提示。微软明确说明 EV 证书也不再保证立即跳过 SmartScreen；不应仅为此购买 EV。[SmartScreen 说明](https://learn.microsoft.com/windows/apps/package-and-deploy/smartscreen-reputation)。

macOS 需要 Apple Developer ID Application 签名、相应发布配置和 Apple 公证；Windows 证书不能解决 Gatekeeper。当前 macOS 包仍未提供受信任发行者签名/公证，不修改这两个标志。[Apple Developer ID](https://developer.apple.com/developer-id/)。

源码与签名接口依据：[SignTool](https://learn.microsoft.com/windows/win32/seccrypto/signtool)、[微软签名集成](https://learn.microsoft.com/en-us/azure/trusted-signing/how-to-signing-integrations)、[官方 Artifact Signing action](https://github.com/Azure/artifact-signing-action)。
