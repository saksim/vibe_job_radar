# 团队服务、共享配额与跨设备版本

对应 #346 / #9 / #30 / #57。此功能默认关闭，原本机采集、公开来源、私人证据和报告继续使用。用户已将跨设备及服务研发纳入本轮；目前未提供 Linux 主机和域名，因此代码、部署配置、本机受控联调与实际公网部署分别记录。这里不宣称已经上线。

## 可以完成的工作

- 管理员登记工作组、成员、角色和设备。设备凭据最长90天、默认30天，可随时撤销；服务端仅保存随机凭据的哈希。reader 可读获准公开来源；collector 还可使用已获授权的账号资源；admin 另可读本组运行指标。成员管理/发证/删成员由服务主机上的管理员命令完成，网络 API 不允许自授权限。
- 管理员将一个实际使用的采集账号登记为固定资源，给对应成员授权，并在每个设备连接中绑定站点。已登记设备对同一工作组/资源共享持久额度、冷却和发布方更严规则。服务器决定资源归属，客户端不能用自报成员/工作组、另一个资源或来源绕过授权。账号名称是管理员维护的别名；服务不验证平台账号身份，不读取招聘密码，也不能约束其他软件或未登记设备。
- 本机原限额先检查，集中服务通过后才提交本机额度并允许上游请求。失败不退款；中央成功而本机提交失败时中央额度仍保留。服务断网、凭据到期、权限撤销或账本损坏会停止当前联网，不自动退回本机模式。离线岗位和旧报告继续可读。
- 经单独分发批准的公开快照可供认证查询，进入原本机任务、Store 与报告链路。公开全文之外，集中服务只接收配额操作、来源范围和同步版本；查询词/地区仅在选择远端查询且同意后发送。密码、Cookie、简历、本人附件和私人报告不上传。
- 同步首先下载完整快照，后续读取新增、修改、本次未出现和确认观察的事件。跨设备共用服务端版本，每个设备独立确认已落盘水位；旧游标绑定设备、来源、版本和期限。部分失败不覆盖上次完整缓存，未出现不等于岗位关闭。同步不覆盖私人岗位库或历史报告。

## 服务端部署

适用模型是单台服务主机、一个 Waitress 进程、持久本地 SQLite 文件、多个 HTTP 客户端。不是网络共享盘、无状态多副本或多机数据库高可用方案。Python >=3.10，服务额外依赖固定为 Waitress 3.0.2。实际目标主机上的 CPU/内存/磁盘、TLS 域名、网络策略和容量需部署时验证。

1. 在准备好的 Linux 主机上建立专用 radar 用户、/opt/vibe-radar/venv 环境和 /var/lib/vibe-radar（属主 radar，0700）。使用本次经过验证的发行源码安装 `pip install '.[service]'`；不要从浮动开发分支直接更新正在运行的服务。
2. 以 radar 用户执行 `vibe-radar-service --data /var/lib/vibe-radar init`。该命令只用于不存在的新库；已有、缺失或损坏的运行库必须恢复完整备份，不能重新 init 清零配额。
3. 复制 [service.example.json](../deploy/team/service.example.json) 为 /etc/vibe-radar/service.json，填写实际域名；连接配置只接受域名，不接受IPv4/IPv6字面地址。应用始终监听127.0.0.1，仅信任该地址反向代理提供的协议头。必须配置 HTTPS 反向代理，否则 API 拒绝请求。
4. 使用 [Caddyfile](../deploy/team/Caddyfile) 配置实际 RADAR_HOST；Caddy 终止 TLS 并转发到本机8766。只开放实际需要的80/443，不暴露后端端口。不要开启包含请求内容、Authorization 或查询文本的调试/访问日志。
5. 审阅并安装 [systemd 服务](../deploy/team/vibe-radar-team.service)，再启动。使用 HTTPS /healthz 检查数据库就绪；认证 /v1/team/metrics 给管理员返回本组成员/有效设备/审计数量及进程运行时间。监控工具发现非200、进程退出、磁盘不足或连续刷新失败时通知运营者；本仓库不替用户购买监控或指定第三方消息收件人。

配置采用4工作线程、32连接、16KB请求体、10秒客户端网络超时、20秒连接空闲期限；每进程最多2000次接入/分钟，持久工作组 API 限制为3600次/小时、20000次/日。来源/账号另受原更严配额约束。单服务最多10000条、50MB完整公开快照，单次同步最多5分钟/2000页/50MB；拒绝超出上限，不承诺任意规模或真实生产吞吐。

[Waitress 代理配置依据](https://docs.pylonsproject.org/projects/waitress/en/latest/arguments.html)、[Caddy 反向代理依据](https://caddyserver.com/docs/quick-starts/reverse-proxy)。本次未在用户未准备的主机上执行安装，也未购买云资源。

## 登记成员、资源和设备

下面命令使用示例标识，实际标识由管理员统一维护。同一真实账号必须复用同一个资源，不能按设备重复登记来重置额度。

```sh
vibe-radar-service --data /var/lib/vibe-radar member --tenant research-team --member alice --role admin
vibe-radar-service --data /var/lib/vibe-radar resource --file /etc/vibe-radar/account.json
vibe-radar-service --data /var/lib/vibe-radar grant --tenant research-team --member alice --value research-account
vibe-radar-service --data /var/lib/vibe-radar device --tenant research-team --member alice --device alice-pc --output /private/alice-device.json
```

资源文件见 [示例](../deploy/team/resource.example.json)。origins 必须是适配器实际访问且管理员核对过的精确来源，示例不是所有动态请求的完整认证。未登记来源会停止；不会因此扩大原采集契约。资源创建后不允许更换站点、来源集合或放宽限额，避免把已有资源改成另一账号或退款。需要扩展配置时先停止任务，由管理员保留原配额并评审迁移。

公开本机目录和单条案例共享 site=greenhouse_public_example，给这个资源登记两个固定接口 origin（https://boards-api.greenhouse.io、https://api.ashbyhq.com），limits.request_interval=30、requests_hour=12、requests_day=24；其余限额沿用示例。中央服务也强制保留这组公开目录上限，管理员不能通过填写通用平台的较宽限额放松它。这不授予任何分发权。配置了团队模式但未绑定的平台会明确停止，管理员应列全需要的站点。

凭据只写入指定的独占文件，不打印令牌。经安全渠道交给对应设备后撤下中转副本；不要把凭据提交 Git、贴进聊天、Issue 或浏览器 URL。新设备需单独发证。换证使用新 device 标识，撤销旧设备并重启客户端。

```sh
vibe-radar-service --data /var/lib/vibe-radar revoke --tenant research-team --device alice-pc
vibe-radar-service --data /var/lib/vibe-radar member --tenant research-team --member alice --role reader --disable
```

## 本机安装与普通使用

管理员准备 [连接配置](../deploy/team/client.example.json) 和设备凭据两个文件。停止该工作区现有工作台、worker及采集任务后执行：

```sh
vibe-radar-team --workspace /path/to/workspace install --config client.json --credential alice-device.json
```

Windows便携程序支持同一安装动作：

```powershell
.\VibeJobRadar.exe --workspace D:\radar-data --team-install client.json --team-credential alice-device.json
```

随后正常启动工作台。首页“团队服务与跨设备同步”可核对当前权限、同步目录和查看水位；展开面板只读本机设置，按按钮才联网。同步在后台执行，不占用原采集操作锁。普通查询仍使用原来源选择和报告入口，不在网页手工填 API Key 或令牌。

安装先写入私有临时目录，全部文件完成后再一次性启用连接；中断不会激活半份连接。异常断电可能留下同工作区的 .team-install-* 私有暂存目录，可在停止程序后核对并移除，不要将其打包。团队请求沿用该工作区的代理/加密解析许可与网络会话规则。

Windows将凭据保存为当前用户DPAPI密文；其他系统使用0700目录/0600文件，未声称磁盘加密。运行实例冻结配置与凭据摘要，更换文件后会停止而非悄悄换身份；重启后采用新配置。已知损坏连接不能被当作未配置服务自动回到直连。

默认 use_public_service=false，只给已有本机采集加入共享额度。需要认证公开服务时由管理员填写已批准的 public_sources 并设置 true；来源定义必须符合 PublicSource 契约，分发许可不能由服务响应自行加入。现有本机每日计划/队列仍限原 local_direct 模式，改变为远端会暂停原条件，不暗中迁移计划。服务端定时刷新见下节。

## 来源发布、刷新、撤回和新鲜度

发布文件为 schema_version=1、sources（PublicSource完整定义列表）、jobs（原公开查询完整记录列表）和 observed_at（来源ID → 带时区的实际确认时间）。缺少确认时间的导入只保留数据，不获新鲜度资格；导入旧快照不会把上传时间冒作发布方确认。每个来源必须有单独的 distribution_approved=true 和实际使用条件；本机三来源默认全部未授权分发。不要把真实私人报告、登录态或个人材料当成快照输入。先检查并取得所需分发授权，工具只执行运营标志，不代替授权判断。

```sh
vibe-radar-service --data /var/lib/vibe-radar publish --snapshot approved-public-snapshot.json
vibe-radar-service --data /var/lib/vibe-radar grant --tenant research-team --member alice --value greenhouse_anthropic --source
```

对于已有固定来源，可使用相同结构、jobs=[] 的分发批准文件，执行一次受控刷新：

```sh
vibe-radar-service --data /var/lib/vibe-radar refresh --approval approved-anthropic.json --source greenhouse_anthropic
```

它复用原固定接口、本机网络策略、30秒/12每小时/24每日额度、ETag与结构校验，再发布完整快照。失败、硬拒绝或带刷新错误的旧缓存不能成为新确认；确认来源之前不请求发布方。三种固定来源分别审查；任意URL不被接受。可审阅 [每日刷新服务](../deploy/team/vibe-radar-refresh@.service)和[定时器](../deploy/team/vibe-radar-refresh@.timer)，只为已有批准文件的来源启用。定时器默认未安装或启用；同站更严限制仍可能阻止某次刷新，运营者应检查退出码和失败原因。

超过7天没有确认观察的来源停止新的服务端查询/同步，状态保留原观测日期。立即撤回分发：

```sh
vibe-radar-service --data /var/lib/vibe-radar withdraw --source greenhouse_anthropic
```

撤回删除该来源当前服务数据和增量正文，旧游标失效。其他设备已下载的数据及私人旧报告不会远程删除，也不能声称已完成撤回后的所有设备清理。重新批准发布须重新取得完整快照。

## 留存、备份与回退

配额不因设备撤销、成员删除或应用升级退款。原账本保留最近两天访问及最长366天发布方窗口；分发变更保留30天且最多10万事件，超过窗口的客户端明确要求完整同步。审计只含动作和不透明标识，保留90天且最多10万条，不存查询文本；过期清理同时在后续读取、健康检查和备份前执行，不要求再发生管理事件。到期令牌停止访问；管理员可执行 erase-member 删除其成员/设备/授权/水位及有关审计，资源配额保留。

停止服务和刷新任务，确认没有管理员并发写入，再运行 `backup --output /private/backup-directory`，会用SQLite备份接口保存control/rates/catalog三个库。备份包含凭据哈希和公开全文，应按相同权限保护；运营者为备份另定保留期并删除过期副本。恢复时整组恢复到停止的服务，不能只恢复较旧rates库制造额度。首次升级前保留工作区和服务完整备份。回退新服务须停止团队联网任务；保留中央账本与本机数据，不以移除.team连接规避共享限制。

## 实际验证记录

对应PR记录本次提交的测试、CI与浏览器结果。两客户端测试使用独立工作区、真实本机HTTP、人工JD和注入时钟；不是两个真实网络设备、生产TLS域名或实站账号认证。服务进程检查使用实际Waitress；反向代理TLS终止、Linux/systemd现场、费用、真实容量、来源许可和公网部署仍需可用目标后验证。没有用单次通过代替24小时运行或人工JD质量验收。
