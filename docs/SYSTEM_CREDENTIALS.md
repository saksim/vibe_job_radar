# 系统密钥库与工作区凭据（0.12.0）

Linux/macOS 可以在工作台保存本机 HTTP Basic、Digest、NTLMv2 和 SOCKS5 的代理凭据；团队设备凭据可明确选择加密安装，或从旧 owner-file 格式迁移。Windows 继续使用原 DPAPI。网站登录、Cookie、代理协议和原有采集、完整 JD、报告功能没有改变。

## 使用

完整 Linux/macOS 程序包包含所需组件。Python 安装方式另安装 secure-store 可选依赖，使用已核验的本地 wheel 路径加 [secure-store]，例如 python -m pip install './vibe_job_radar-0.12.0-py3-none-any.whl[secure-store]'；本项目未发布 PyPI。Linux 需要运行中的 Secret Service，以及已配置、已解锁的默认密钥库；macOS 需要默认钥匙串已解锁并允许当前程序访问。无桌面的 Linux 不一定提供这些服务。

在本工作区代理区选择认证方式，输入代理账号、密码并同意说明后保存。保存不会连接代理或招聘网站，状态页不回显凭据。更换后停止并重开原会话；后台任务再次读取凭据时使用同一系统账号和工作区位置。已运行会话内的旧凭据不会因锁定密钥库自动从内存清除。

团队安装沿用原命令，增加明确选项：

    VibeJobRadar --workspace /原工作区 --team-install /管理员配置.json --team-credential /设备凭据.json --team-encrypted

既有团队工作区可在首页“团队服务与跨设备同步”中阅读迁移说明后点击“同意说明，迁移为系统加密”，或执行：

    VibeJobRadar --workspace /原工作区 --team-protect-credential
    vibe-radar-team --workspace /原工作区 protect-credential

Python 的团队安装命令对应 install --encrypted。迁移只修改本机凭据，不联系团队服务；成功后停止任务并重启工作台。旧连接进程检测到文件变化后停止请求。管理员提供的原凭据文件和历史备份不会自动删除，仍需按原流程保管。原 owner-file 继续可读，未选择加密的旧安装方式保留。

## 错误和恢复

- 缺少服务、依赖或默认密钥库：先在操作系统中配置，再重试；应用不会自动创建默认库或回退明文。
- 锁定或访问未获授权：在系统密钥库中解锁或授权当前程序。后台操作不主动弹出解锁窗口，超过十秒停止并收回辅助进程。
- 工作区移动、账号变更或原密钥丢失：使用原位置/账号，或重新输入代理凭据；团队设备按原管理员安装流程恢复。不在读取失败时生成替代密钥。
- 文件篡改、错误入口或模式：认证解密失败，停止当前入口的新请求。保存或迁移失败时原配置保留。
- 清除代理凭据只移除当前密文，并保持该入口停止。明确选择自动/匿名方式才回到其他路径；原备份中的密文仍需自行管理。

## 存储与版本

Linux 使用 SecretStorage 3.5.0 的 Secret Service；只操作现有默认集合与本应用、当前工作区标识匹配的项目，不使用临时 session 集合、其他后端或明文 D-Bus 会话。macOS 使用 Security.framework 的 SecItem 接口，只操作默认钥匙串中本应用的项目，保留系统访问控制。

系统密钥库保存每工作区随机 256 位主密钥。网络文件和团队文件仅保存随机 nonce 与 AES-GCM 认证密文，绑定规范工作区路径、用途、代理模式/入口或团队服务 origin。密钥通过短生命周期进程的私有管道返回，不放入命令行、环境或磁盘文件。写入密文与创建密钥受原工作区写锁保护；读取从不创建或轮换密钥。清除凭据不删除工作区主密钥，也不宣称清除全部内存或备份副本。

POSIX 代理使用网络 schema 12 和独立同意版本。旧 DPAPI schema 8/11 继续读取；旧程序会拒绝 schema 12。需要回退时先用新程序明确选择自动/匿名模式，降至原 schema 2。团队新增 system-keyring-aesgcm-v1；旧程序会拒绝该格式。团队降级须按管理员原换证/重装流程处理，不自动解密导出为旧明文文件。

## 验证范围

原生 CI 用独立 D-Bus/临时 GNOME Keyring 或临时 macOS Keychain，在源码入口与实际解压程序各验证四种代理保存、独立重开、团队迁移/加密安装、锁定失败、篡改、搬移、清除和回退。人工密钥库只在一次性 GitHub 运行器创建，恢复原默认钥匙串设置。实际包仍不代表个人干净设备、企业环境或 Gatekeeper 验收。

macOS 包没有发布者签名/公证；更换程序包后系统可能要求重新授权访问原密钥。本项不绕过访问控制，也不承诺所有第三方 Secret Service 实现或无桌面系统兼容。生产主机/域名、真实代理和人工材料条件继续保留。

参考：[SecretStorage API](https://secretstorage.readthedocs.io/en/latest/collection.html)、[Secret Service 会话协议](https://specifications.freedesktop.org/secret-service/latest/ch07s03.html)、[Apple SecItem 接口](https://github.com/apple-oss-distributions/Security/blob/main/keychain/headers/SecItem.h)。
关联 [#403](https://github.com/saksim/vibe_job_radar/issues/403)、[#23](https://github.com/saksim/vibe_job_radar/issues/23)、[#30](https://github.com/saksim/vibe_job_radar/issues/30)、[#57](https://github.com/saksim/vibe_job_radar/issues/57)。
