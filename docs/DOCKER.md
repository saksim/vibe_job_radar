# Docker 服务器工作台

本入口让一个工作区的拥有者在浏览器中使用远程服务器上的 Vibe Job Radar。服务器保存岗位、个人材料、证据和报告；浏览器并不会把这些数据自动保存在访问者自己的电脑。首次部署请使用新的专用数据卷，由拥有者决定哪些材料上传。

这是单拥有者、单进程、单副本部署。拥有者的多个会话访问同一工作区，拥有同样权限；没有多人账号、租户隔离或成员级私人报告权限。原桌面工作台、CLI 和[团队设备服务](TEAM_SERVICE.md)继续保留，团队设备凭据不是此工作台的登录密码。

## 本期能力

| 服务器浏览器入口 | 范围 |
| --- | --- |
| 岗位文本导入、已支持的公开来源查询、原报告链路 | 复用原业务服务和数据模型；来源许可、预算、robots、正文证据门槛继续生效 |
| 证据映射、质量审阅、历史报告与下载 | 内容属于服务器工作区；报告下载才产生浏览器端副本 |
| 已明确启用的公开计划和待办队列 | 由服务器进程运行，关闭网页不停止；关停服务器、中断或条件变化仍按原规则暂停和恢复 |
| 高级页面中的非类别采集 | 拥有者确认当前有界批次后由应用服务推进，关闭网页不停止；可暂停并查看持久状态。服务退出或中断后保留检查点并暂停，重新确认原批次后继续，不自动重放不确定请求 |
| 报告生成 | 普通分析、演示和个人证据报告由应用后台完成；页面关闭后可重开查看当前任务和报告。中断只记录状态，不自动重放；不要在报告写入期间升级实例 |
| 会话 | 文件密码登录，Cookie 会话、同源检查和请求令牌；退出或服务重启使相应会话失效 |
| 健康检查 | `GET /healthz` 只返回 `{"ok": true}`，不要求登录，不返回工作区、任务或个人材料 |

镜像只安装 `service` 依赖，不包含 Playwright、Chrome、Edge 或桌面密钥环。服务器入口不提供本机浏览器采集、招聘平台登录接管、Windows 自启动、桌面安装包自动升级和网络设置修改；需要这些能力时继续使用桌面入口。首期没有把这些桌面能力搬到服务器，也没有删除其底层实现。

## 数据和配置边界

- `/data` 挂载整个命名卷。除 `jobs.sqlite` 外还包含导入、报告、证据、质量审阅、公开缓存、配额、计划、队列及其他工作区状态，不能只持久化岗位库。
- 同一卷只能由一个工作台实例写入。不要启动第二个副本或对同一卷再启动独立 worker；不要换成 NFS/SMB 共享数据库。原本机 SQLite、任务所有权、预算和失败不退款语义不变。
- `RADAR_ORIGIN` 是浏览器使用的精确外部来源：协议、主机及非默认端口，不含路径、末尾斜杠、查询或账号。远程必须是 HTTPS；本机开发示例使用 `http://localhost:8080`。
- `RADAR_PASSWORD_FILE` 是仓库和构建上下文以外的绝对文件路径；文件只保存一行高强度随机密码（16～512 字符）。Compose 以 secret 挂载到 `/run/secrets/radar-password`，密码本身不放环境变量、命令行或 `.env`。
- 容器以 UID/GID `10001:10001` 运行，根文件系统只读，仅 `/data` 和临时 `/tmp` 可写；启用 init、移除 capabilities、禁止新增权限。secret 的宿主文件必须允许 UID 10001 读取。Compose 的本地文件 secret 是文件挂载，不能依赖 `uid/gid/mode` 自动修正宿主权限。
- 容器内会话保存在内存，重启后重新登录。修改密码文件后重建容器使新密码生效；数据卷和原报告保持不变。
- 外部来源请求由服务器发出，受服务器出口网络和已有来源许可约束。启动、打开页面或健康检查本身不构成采集授权。

## 在 Linux Docker 主机上准备

需要 Docker Engine 和 Compose、可写本地数据卷，以及远程部署时可用的域名、HTTPS 反向代理与出口网络。下列是部署操作说明，不代表已经在目标服务器执行。

先在仓库外生成密码文件。此示例只创建不存在的文件，不覆盖已有密码，也不打印密码：

```sh
sudo install -d -m 0700 -o 10001 -g 10001 /etc/vibe-radar/secrets
sudo python3 - <<'PY'
import os
import secrets

path = "/etc/vibe-radar/secrets/radar-password"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
with os.fdopen(fd, "w", encoding="utf-8") as output:
    output.write(secrets.token_urlsafe(32) + "\n")
os.chown(path, 10001, 10001)
PY
```

通过受控的本机方式读取密码并保存在拥有者的密码管理器中。Docker Engine 必须有权读取该外部文件并挂载；不要把密码文件复制进项目来绕过路径或权限错误。

在经过审核的发行源码根目录配置：

```sh
cd deploy/workbench
cp .env.example .env
# 编辑 .env 中的 RADAR_ORIGIN、RADAR_PASSWORD_FILE 和正式镜像版本/digest。
docker compose config
docker compose pull
docker compose up -d --no-build
docker compose ps
```

默认仅发布宿主机 `127.0.0.1:8080`，可在该主机浏览器访问 `http://localhost:8080/login`。不要用 `127.0.0.1` 替换已配置为 `localhost` 的浏览器地址，两者是不同来源。新命名卷从镜像中的 `/data` 初始化权限；若换用已有卷或 bind mount，部署者需预先保证整个数据目录由 UID/GID 10001 读写。

正式镜像名称为 `ghcr.io/saksim/vibe_job_radar`；Compose 默认选定 `0.20.2`，不要求部署者下载源码重新构建镜像。版本标签在发布器中拒绝覆盖；真正由内容固定的地址是 `ghcr.io/saksim/vibe_job_radar@sha256:<本次正式多架构 index digest>`，可将发行记录中的准确地址填入 `RADAR_IMAGE`。`latest` 会随后续完成匿名验收的版本移动，不用于固定一次部署。

正式 manifest 同时列出 `linux/amd64` 和 `linux/arm64`，Docker 根据主机架构选择镜像。两个架构分别由原生 runner 构建、实际启动和验证，不把 QEMU 构建或架构标记当作原生运行验收。Dockerfile 从发行源码安装 `.[service]`；候选 CI 先解析一次官方 Python 基础镜像 index digest，再给两平台提供同一个固定基础输入。

需要保留本地源码构建方式时，显式加覆盖文件：

```sh
docker compose -f compose.yaml -f compose.build.yaml build
docker compose -f compose.yaml -f compose.build.yaml up -d
```

覆盖文件使用独立 `RADAR_BUILD_IMAGE`（默认 `vibe-job-radar-workbench:local`），不覆盖正式镜像引用。普通 `compose.yaml` 没有 `build` 字段。

## 远程 HTTPS

将 `.env` 的 `RADAR_ORIGIN` 改成实际来源，例如 `https://radar.example.com`，并重建服务配置。此名称只是示例，需由部署者准备域名和 DNS。

[独立 Caddy 配置](../deploy/workbench/Caddyfile)用于运行在同一 Docker 宿主机上的 Caddy：在 Caddy 的服务环境设置 `RADAR_HOST=radar.example.com`，让它监听 80/443、管理实际域名证书，并反向代理到 `127.0.0.1:8080`。保留浏览器原始 Host；工作台不根据客户端提供的转发头推断可信来源。若把 Caddy 也放到容器，需要另外设计内部网络，不能直接沿用此宿主机地址示例。

工作台和此反向代理都保留原 `2,000,000` 字节请求体上限，具体导入接口仍可有更小的原有限制。不要误用团队 API 的 `deploy/team/Caddyfile`（16 KB）。不要把后端 8080 发布到公网，或记录含密码、请求体、Cookie 的调试日志。确认 HTTPS 页面、登录、登出和报告下载后再开放给拥有者使用。

## 停止、升级与恢复

服务器镜像更新由部署者管理，网页不会下载或执行桌面安装包。源码仍按 dev → release → main 和正式 Tag 晋升；部署时选定已公开且验证过的镜像版本或 digest，记录准确摘要。

升级前先在页面停止或等待任务完成，暂停不希望自动继续的计划和待办，然后 `docker compose stop`。停止后备份整个 `radar-data` 卷和外部部署配置/secret，保留文件所有权；不要在仍写入时只复制某一个 SQLite 文件。备份中可能含私人材料，应按同样访问边界保存。

在核对发行说明与数据格式兼容性后，更新 `RADAR_IMAGE`，执行 `docker compose pull` 和 `docker compose up -d --no-build` 复用原卷。不要使用 `down -v` 或删除配额/队列库来“修复”启动问题。回退前检查数据兼容性；不能靠恢复较旧配额或授权状态来退款或恢复已撤销权限。没有验证过的旧版本恢复流程不得视为可用回退。

`/healthz` 只证明进程能响应，不证明外部来源、文件系统容量、完整报告或公网 TLS 已验收。实际主机仍需检查退出行为、卷持久化、备份恢复、HTTPS 和所需公开来源；这些验收不能用一次 Compose 配置通过替代。

## 0.17.0 历史验证边界

0.17.0 轮已在 Docker Desktop 的 Linux Engine 29.2.1、Compose 5.0.2 上实际构建并运行隔离容器。镜像内为 Python 3.12.14、Waitress 3.0.2 和当时的 0.17.0 代码。使用单独命名卷、随机临时 secret 和本机 127.0.0.1 端口完成 53 项链路/部署断言：非 root、只读根文件系统、移除能力、健康检查、拥有者登录、自编离线 JD 导入、原报告生成与下载，以及替换容器后的数据和会话行为。没有访问真实招聘来源或挂载用户工作区。

该轮首个功能镜像在版本号更新前捕获了 0.16.0，随后用 0.17.0 候选源码构建替换镜像并复用同一卷。原 1 条岗位记录、报告 manifest 和 requirements.jsonl 摘要保持一致，旧会话返回 401；两次停止均为 exit 0。这里的首个镜像包含该轮服务器功能，并不是此前发布的 0.16.0 安装包；该检查证明此次容器替换与数据保留，不能外推为所有历史版本或数据格式迁移验收。

该轮最终本地验证镜像为 `vibe-radar-architecture-test:20261023`，镜像摘要 `sha256:1dca1ef26ed81b1da3d10b8837e2687a99c187cac94efd5bd1208e0ab2537f5f`。此标签未推送到公开镜像仓库。测试容器、独立卷、网络和临时 secret 已清理，镜像与脱敏验证记录保留。同轮 Windows 真实 HTTP 链路 34 项、浏览器页面 17 项检查也通过，具体证据随本轮 PR 记录。

公网 HTTPS、真实 Linux 服务器部署、Caddy 证书签发与真实公开来源连通性尚未验收；配置示例和本机容器通过不能替代这些条件。用户真实材料、原报告以及生产数据升级也未在本轮隔离测试中使用。

## 正式镜像的发布与公开边界

容器候选工作流只在功能 PR 到 `dev` 以及 `main` push 运行；PR 显式检出功能 head，主干候选检出准确 main。两架构的小证明记录源码提交/tree、构建输入摘要、基础镜像 digest、实际 image config digest、归档 SHA256，以及真实 HTTP 导入、原报告、同卷重启持久化和会话失效检查。旧桌面/Python 的 11 类资格检查保留。

维护者显式运行 `prepare-container-release`，选择准确 main 首轮成功候选。发布器还要求现有 CI 成功，且 `vVERSION`、`python-vVERSION`、`desktop-vVERSION` 三个正式 Release 和实际 Git Tag 都指向同一 main 提交；只加载已验证的候选归档，不在发布时重新构建。它创建版本及完整源码 SHA 标签，并保留多架构 index 和各平台 digest 证明，不修改 Windows 的八项升级资产。

GHCR 新建的个人 package 默认是 private，仓库公开不等于 package 公开。发布者需在该 package 的 **Package settings → Change visibility → Public** 完成一次公开；公开后不能再改为 private。工作流使用本仓库 `GITHUB_TOKEN` 的 `packages: write`，不需要把个人 PAT 放入镜像或 Compose；若同名包此前未关联仓库，包管理员还需给本仓库 Actions 写入权限。[GitHub 官方权限与可见性说明](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)、[GHCR 身份验证说明](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。

版本推送成功只表示 `published_pending_public_verification`。公开后单独运行 `verify-container-public`：两个原生 runner 分别使用全新空 Docker 配置匿名拉取准确 index digest，并在 amd64、arm64 上各实际验证 HTTP、报告、同卷持久化与退出。汇总作业按同一运行、实际成功 job 时间窗和 artifact ID/digest 核对两份小证明；两平台都通过后才能使用写包权限，让 `latest` 指向相同 digest。公开或任一平台匿名验收未完成时，不宣称完整公开交付，也不移动 `latest`。

这里的镜像发行不自动部署生产。真实 Linux 主机、域名、HTTPS、实际数据与生产备份恢复仍须在目标环境核验。本页上一节的 0.17.0 本机隔离容器结果保留为历史证据；0.18.0 镜像实际是否已公开及其准确 digest，以对应正式发行记录和 `container-public-verification` 证明为准。
