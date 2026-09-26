# 历史目标与Issue/PR追溯表

核查日期：2026-09-26。配合[主报告](PROJECT_GAP_AUDIT_2026_09_26.md)使用。以下按原承诺编号核对；不同组有交集，39行不是39个互不重叠的新需求。

原始产品的“真实JD→要求→通用/分岗位能力→本人证据→可量化描述与缺口”对应主报告G02/G03/G04。基础界面/分析已有实现；原22项技术能力不能替代产品结果验收。

状态说明：候选指当前运行PR207或独立标明的PR209；已合并指远端main。GitHub的OPEN/CLOSED/MERGED是管理状态，不能直接推断功能缺失或真实认证。

## 原22项能力与H1—H6

| 编号 | 原目标 | 核查判断 | 对应GAP | 原跟踪 |
|---|---|---|---|---|
| N1 | 自动消费系统/环境静态代理、优先级、NO_PROXY | 已实现基础范围；复杂来源和真实环境仍待验 | G09 | [#23](https://github.com/saksim/vibe_job_radar/issues/23) |
| N2 | Fake-IP解析/连接/信任契约 | 已有限定198.18/15路径；不覆盖所有映射/网络产品 | G09 | [#23](https://github.com/saksim/vibe_job_radar/issues/23) |
| N3 | 网页网络策略、工作区隔离、任务快照、跨入口一致 | 候选已实现；真实环境矩阵未齐 | G07/G09 | [#23](https://github.com/saksim/vibe_job_radar/issues/23)/[#64](https://github.com/saksim/vibe_job_radar/issues/64)/[#81](https://github.com/saksim/vibe_job_radar/issues/81) |
| N4 | 本机匿名SOCKS5数字公网CONNECT | PR32已合并；原PR26已关闭，不重复开发 | 基础范围已完成；远端DNS归N5 | [#25](https://github.com/saksim/vibe_job_radar/issues/25) |
| N5 | 代理认证与远端DNS | 候选支持显式HTTP Basic/SOCKS5认证；远端DNS和其他认证仍缺 | G09 | [#23](https://github.com/saksim/vibe_job_radar/issues/23)/[#75](https://github.com/saksim/vibe_job_radar/issues/75) |
| N6 | PAC适配与受控求值 | 候选有Windows域名PAC/已配置系统URL；WPAD、其他OS/路径规则和现场未全覆盖 | G09 | [#23](https://github.com/saksim/vibe_job_radar/issues/23)/[#109](https://github.com/saksim/vibe_job_radar/issues/109)/[#135](https://github.com/saksim/vibe_job_radar/issues/135) |
| N7 | VM宿主机授权代理入口 | 显式入口与受控隔离验证已有；具体hypervisor/设备现场未验 | G09 | [#23](https://github.com/saksim/vibe_job_radar/issues/23)/[#83](https://github.com/saksim/vibe_job_radar/issues/83) |
| N8 | 实际VPN/TUN/系统网络矩阵 | 有受控与有限公网证据；用户实际环境矩阵未齐 | G09 | [#23](https://github.com/saksim/vibe_job_radar/issues/23) |
| A1 | 网页取得真实公开案例 | PR31已合并，来源/确认/预算/缓存保留 | 基础范围已完成；不代替S1 | [#3](https://github.com/saksim/vibe_job_radar/issues/3) |
| A2 | 失败上下文转浏览器继续 | PR31基础已合并，后续所有权/恢复在候选中；真实异常链路仍待验 | G01/G05 | [#3](https://github.com/saksim/vibe_job_radar/issues/3)/[#9](https://github.com/saksim/vibe_job_radar/issues/9) |
| S1 | 首站正常页面/接口/许可到真实完整报告 | 公开分类已取得正文；正常登录搜索闭环未验通 | G01 | [#9](https://github.com/saksim/vibe_job_radar/issues/9)/[#49](https://github.com/saksim/vibe_job_radar/issues/49)/[#63](https://github.com/saksim/vibe_job_radar/issues/63) |
| S2 | 三站分别30个不同完整岗位及3真实日期 | 猎聘公共路径有90URL/3日期；人审/认证和另外两站不完整 | G05 | [#9](https://github.com/saksim/vibe_job_radar/issues/9)/[#54](https://github.com/saksim/vibe_job_radar/issues/54)/[#55](https://github.com/saksim/vibe_job_radar/issues/55)/[#56](https://github.com/saksim/vibe_job_radar/issues/56) |
| S3 | 无href、iframe、POST、SSO、跨域XHR | 仅部分已审核路径；缺逐站完整适配与现场范围 | G01/G05 | [#9](https://github.com/saksim/vibe_job_radar/issues/9) |
| S4 | 过期/撤销/空列表/下架/结构变化及恢复 | 受控检查和部分公共路径已有；正常账号实站异常未完整验收 | G01/G05/G06 | [#9](https://github.com/saksim/vibe_job_radar/issues/9)/[#52](https://github.com/saksim/vibe_job_radar/issues/52) |
| S5 | 会话连续性与单次正常密码表单 | 会话基础已合并，密码表单及切页等在候选；真账号/跨重启未验 | G01 | [#9](https://github.com/saksim/vibe_job_radar/issues/9)/[#50](https://github.com/saksim/vibe_job_radar/issues/50) |
| R1 | 严格发布方节奏、精确窗口、跨任务/重启共享 | 基础已合并，高级HTTP和登录等待提示已接线；真实等待恢复范围另验 | G05；实现不应重做 | [#27](https://github.com/saksim/vibe_job_radar/issues/27)/[#168](https://github.com/saksim/vibe_job_radar/issues/168)/[#207](https://github.com/saksim/vibe_job_radar/issues/207) |
| R2 | 有限只读重试、退避、权限/限流分类 | 候选已有主文档GET有限重试；实站验证与范围有限 | G05 | [#9](https://github.com/saksim/vibe_job_radar/issues/9)/[#79](https://github.com/saksim/vibe_job_radar/issues/79) |
| R3 | 持久队列、定时、系统登录启动 | 公开目录相关候选已有；真实重登录/24小时长跑、无人登录服务未齐 | G10 | [#9](https://github.com/saksim/vibe_job_radar/issues/9)/[#87](https://github.com/saksim/vibe_job_radar/issues/87)/[#115](https://github.com/saksim/vibe_job_radar/issues/115)/[#120](https://github.com/saksim/vibe_job_radar/issues/120)/[#131](https://github.com/saksim/vibe_job_radar/issues/131) |
| R4 | 跨工作区/设备账户全局配额与多人归属 | 同设备同工作区已有；跨设备、跨工作区账号级约束/权限未完成 | G11 | [#9](https://github.com/saksim/vibe_job_radar/issues/9)/[#89](https://github.com/saksim/vibe_job_radar/issues/89)/[#93](https://github.com/saksim/vibe_job_radar/issues/93) |
| R5 | 增量水位、结构监测、新鲜度 | 本地目录差分/ETag已有；全来源逐条增量与跨设备水位未完成 | G11 | [#9](https://github.com/saksim/vibe_job_radar/issues/9)/[#30](https://github.com/saksim/vibe_job_radar/issues/30)/[#101](https://github.com/saksim/vibe_job_radar/issues/101) |
| Q1 | CI实际执行与当前提交验收 | CI已可真实执行；部分Windows不稳定根因未解决 | G06 | [#28](https://github.com/saksim/vibe_job_radar/issues/28)/[#126](https://github.com/saksim/vibe_job_radar/issues/126)/[#179](https://github.com/saksim/vibe_job_radar/issues/179)/[#202](https://github.com/saksim/vibe_job_radar/issues/202) |
| Q2 | 源码绑定验证、候选构建 | PR34已合并，候选反复独立验证；新209未通过全部资格，正式交付待收口 | G06/G07 | [#28](https://github.com/saksim/vibe_job_radar/issues/28)/[#34](https://github.com/saksim/vibe_job_radar/issues/34)/[#91](https://github.com/saksim/vibe_job_radar/issues/91) |
| H1 | 最小公开查询/响应契约 | 严格字段、来源/身份/正文/时间校验已有 | 基础范围已完成 | [#30](https://github.com/saksim/vibe_job_radar/issues/30) |
| H2 | 来源目录及各自使用/运行条件 | Anthropic基础、Cloudflare/Cursor候选；三站专用能力与服务分发仍单列 | G05/G12 | [#30](https://github.com/saksim/vibe_job_radar/issues/30)/[#95](https://github.com/saksim/vibe_job_radar/issues/95)/[#118](https://github.com/saksim/vibe_job_radar/issues/118) |
| H3 | 来源审计与目录变化 | PR35已合并，候选有ETag/独立确认时间；不等于所有来源增量 | G11 | [#30](https://github.com/saksim/vibe_job_radar/issues/30)/[#101](https://github.com/saksim/vibe_job_radar/issues/101) |
| H4 | 本地缓存、停止/续接、计划/所有权/待办 | 已实现大量候选功能；实际长跑及跨设备部分未齐 | G10/G11 | [#30](https://github.com/saksim/vibe_job_radar/issues/30)/[#68](https://github.com/saksim/vibe_job_radar/issues/68)/[#87](https://github.com/saksim/vibe_job_radar/issues/87)/[#89](https://github.com/saksim/vibe_job_radar/issues/89)/[#124](https://github.com/saksim/vibe_job_radar/issues/124)/[#131](https://github.com/saksim/vibe_job_radar/issues/131) |
| H5 | 可选生产公开服务 | 用户已明确后置；当前不要求购买/部署服务器 | G12（后置） | [#30](https://github.com/saksim/vibe_job_radar/issues/30) |
| H6 | 普通用户安装、升级、迁移、回滚 | 源码与Windows便携候选已有；正式签名发行/自动升级/其他OS包未交付 | G07/G08 | [#30](https://github.com/saksim/vibe_job_radar/issues/30)/[#57](https://github.com/saksim/vibe_job_radar/issues/57)/[#91](https://github.com/saksim/vibe_job_radar/issues/91) |

## D01—D11专项验收

| 编号 | 原目标 | 核查判断 | 对应GAP | 原跟踪 |
|---|---|---|---|---|
| D01 | 首个业务失败诊断/访问契约 | 诊断代码和证据已有；具体实站失败/部分历史根因仍未收敛 | G01/G06 | [#47](https://github.com/saksim/vibe_job_radar/issues/47) |
| D02 | 原生浏览器网络与业务观察 | 基础已合并；显式实验，逐站实站范围尚未认证 | G05 | [#48](https://github.com/saksim/vibe_job_radar/issues/48) |
| D03 | 正常登录会话→相关完整JD→原报告 | 基础接续合并；完整真账号闭环未验通 | G01 | [#49](https://github.com/saksim/vibe_job_radar/issues/49) |
| D04 | 正常自动登录、保存会话、过期恢复 | 受控实现与局部交互修复已有；现场认证仍缺 | G01 | [#50](https://github.com/saksim/vibe_job_radar/issues/50) |
| D05 | 猎聘分页、小批、岗位身份关联 | 候选实现，公开分类已有1/2页观察；正常搜索及第3—5页现场未齐 | G01/G05 | [#51](https://github.com/saksim/vibe_job_radar/issues/51) |
| D06 | 持久检查点、部分恢复、跨后端额度 | 候选和公共路径部分可用；真账号任务退出/恢复验收未完整 | G01/G10 | [#52](https://github.com/saksim/vibe_job_radar/issues/52) |
| D07 | 本批漏斗、正文证据链、零结果解释 | 代码及真实公共报告已有；同一真账号任务到本人证据仍待验收 | G01/G04/G05 | [#53](https://github.com/saksim/vibe_job_radar/issues/53) |
| D08 | 跨日真实验收与首站受控默认 | 三个实际日期已补；人工身份/全文、阈值、正常账号认证未完成 | G05 | [#54](https://github.com/saksim/vibe_job_radar/issues/54) |
| D09 | BOSS专用适配与独立认证 | 通用DOM/107链接观察已有；无完整JD，专用链路未认证 | G05 | [#55](https://github.com/saksim/vibe_job_radar/issues/55) |
| D10 | 51job契约澄清、专用适配/认证 | robots返回HTML原因未清；无完整JD认证 | G05 | [#56](https://github.com/saksim/vibe_job_radar/issues/56) |
| D11 | 矩阵、回滚、旧范围收口 | 矩阵/回退代码已有；多个当前状态漂移，主干交付/实站收口未完成 | G07/G08 | [#57](https://github.com/saksim/vibe_job_radar/issues/57) |

## 全部104个Issue索引

104个Issue全部映射到一个主题：92开放、12关闭。下表用于追溯原范围；每个功能/验收的判断以上述矩阵和主报告为准，不把未勾选的历史清单机械计作新缺口。

### 产品与体验

| Issue | GitHub状态 | 历史标题 |
|---|---|---|
| [#1](https://github.com/saksim/vibe_job_radar/issues/1) | CLOSED | feat: 打通普通用户从零使用、真实 JD 接入与可观测报告闭环 |
| [#3](https://github.com/saksim/vibe_job_radar/issues/3) | OPEN | roadmap: 产品闭环总索引——22项开放能力分类与分期验收 |
| [#4](https://github.com/saksim/vibe_job_radar/issues/4) | CLOSED | feat: 多平台自动采集流水线——检索、许可正文获取、断点续跑与来源级审计 |
| [#5](https://github.com/saksim/vibe_job_radar/issues/5) | CLOSED | feat: 证据全界面化——原文复核、附件、量化指标、精确映射与个人报告 |
| [#7](https://github.com/saksim/vibe_job_radar/issues/7) | CLOSED | docs: 零基础上手指南、采集能力分级与真实爬虫后续路线 |
| [#10](https://github.com/saksim/vibe_job_radar/issues/10) | CLOSED | ux: 采集表单按路线引导、字段来源案例与零外部请求预检 |
| [#12](https://github.com/saksim/vibe_job_radar/issues/12) | CLOSED | chore: main 收口、零基础文档同步与已合并分支清理 |
| [#14](https://github.com/saksim/vibe_job_radar/issues/14) | CLOSED | feat: 引导式站内搜索→岗位清单→详情采集，正常登录与人工接管、共享限频 |
| [#16](https://github.com/saksim/vibe_job_radar/issues/16) | CLOSED | chore: 合并0.2.0验收版本、修复合并前审查并清理非main分支 |
| [#17](https://github.com/saksim/vibe_job_radar/issues/17) | CLOSED | fix: 浏览器安装不等于就绪——识别错误 Chromium 包、分阶段诊断并验证真实启动 |
| [#19](https://github.com/saksim/vibe_job_radar/issues/19) | CLOSED | chore: 合并浏览器就绪修复、处理四条审查并清理非main分支 |
| [#20](https://github.com/saksim/vibe_job_radar/issues/20) | CLOSED | fix: HTTP重定向与预算解释、真实公开岗位案例（本期已验收） |
| [#22](https://github.com/saksim/vibe_job_radar/issues/22) | CLOSED | chore: 合并重定向与真实案例修复、同步剩余需求并清理非main分支 |
| [#77](https://github.com/saksim/vibe_job_radar/issues/77) | OPEN | fix: 向导初始化完成前禁用动作，避免首次点击被吞掉 |
| [#99](https://github.com/saksim/vibe_job_radar/issues/99) | OPEN | fix(P1): 统一首页和向导的登录能力说明，消除旧版矛盾提示 |
| [#194](https://github.com/saksim/vibe_job_radar/issues/194) | OPEN | docs: 同步首次取数指南、登录会话与后台执行说明 |

### 网络与访问策略

| Issue | GitHub状态 | 历史标题 |
|---|---|---|
| [#23](https://github.com/saksim/vibe_job_radar/issues/23) | OPEN | fix: 代理/TUN/VPN/虚拟机网络兼容——统一连接策略、加密DNS与可验证诊断 |
| [#25](https://github.com/saksim/vibe_job_radar/issues/25) | OPEN | feat: 第二阶段本机 SOCKS5 连接；保留公网目标验证和不直连回退 |
| [#27](https://github.com/saksim/vibe_job_radar/issues/27) | OPEN | fix(P0): 允许的慢速采集采用发布方节奏（实现已合并，历史记录保留） |
| [#64](https://github.com/saksim/vibe_job_radar/issues/64) | OPEN | fix: 高级采集的搜索、正文与 feed 未应用工作区网络偏好 |
| [#65](https://github.com/saksim/vibe_job_radar/issues/65) | OPEN | fix: Windows 工作台拒绝 POST 时偶发连接中止，客户端收不到 403 |
| [#75](https://github.com/saksim/vibe_job_radar/issues/75) | OPEN | feat(N5): 为明确配置的本机 HTTP/SOCKS5 代理增加隔离认证 |
| [#81](https://github.com/saksim/vibe_job_radar/issues/81) | OPEN | feat: 工作区网页代理偏好、跨入口一致策略与安全回退 |
| [#83](https://github.com/saksim/vibe_job_radar/issues/83) | OPEN | feat(network): 显式 VM 宿主机代理入口与隔离路径验证 |
| [#85](https://github.com/saksim/vibe_job_radar/issues/85) | OPEN | fix(acquisition): 高级HTTP与浏览器共用有界robots规则匹配 |
| [#109](https://github.com/saksim/vibe_job_radar/issues/109) | OPEN | feat(N6): 明确导入的Windows域名PAC与无损选路结果 |
| [#135](https://github.com/saksim/vibe_job_radar/issues/135) | OPEN | feat(N6): 明确确认后自动使用 Windows 已配置的 PAC 脚本地址 |
| [#168](https://github.com/saksim/vibe_job_radar/issues/168) | OPEN | fix(P0): 高级站点HTTP接入工作区持久限额与冷却 |
| [#186](https://github.com/saksim/vibe_job_radar/issues/186) | OPEN | fix(P0): robots前导通配符被误判为整份不可用 |

### 采集与站点认证

| Issue | GitHub状态 | 历史标题 |
|---|---|---|
| [#9](https://github.com/saksim/vibe_job_radar/issues/9) | OPEN | feat: 逐站浏览器采集器与韧性调度——首站实测、正常会话、动态页面和限流恢复 |
| [#45](https://github.com/saksim/vibe_job_radar/issues/45) | OPEN | fix(P0): 首站页面加载闭环——请求头重复、猎聘资源缺失与三站 robots 分流 |
| [#46](https://github.com/saksim/vibe_job_radar/issues/46) | OPEN | epic(P0)[E00]: 真实数据获取整改——原生浏览器、专用适配、单条到多站实测 |
| [#47](https://github.com/saksim/vibe_job_radar/issues/47) | OPEN | feat(P0)[D01]: 首个业务失败诊断与原生采集访问契约 |
| [#48](https://github.com/saksim/vibe_job_radar/issues/48) | OPEN | feat(P0)[D02]: 可切换原生浏览器后端、最小安全策略与业务响应观测 |
| [#49](https://github.com/saksim/vibe_job_radar/issues/49) | OPEN | feat(P0)[D03]: 猎聘正常登录会话 → 自动获取真实完整 JD → 原有报告 |
| [#50](https://github.com/saksim/vibe_job_radar/issues/50) | OPEN | feat(P0)[D04]: 正常自动登录、本地会话复用与过期恢复 |
| [#51](https://github.com/saksim/vibe_job_radar/issues/51) | OPEN | feat(P0)[D05]: 猎聘列表分页、小批采集与岗位身份关联 |
| [#52](https://github.com/saksim/vibe_job_radar/issues/52) | OPEN | feat(P0)[D06]: 任务检查点、部分结果恢复与跨后端共享配额 |
| [#53](https://github.com/saksim/vibe_job_radar/issues/53) | OPEN | feat(P0)[D07]: 本批取数漏斗、正文证据链与零结果分层解释 |
| [#54](https://github.com/saksim/vibe_job_radar/issues/54) | OPEN | test(P0)[D08]: 猎聘跨日期真实样本验收与首站受控默认切换 |
| [#55](https://github.com/saksim/vibe_job_radar/issues/55) | OPEN | feat(P1)[D09]: BOSS 专用数据适配与独立实站验收 |
| [#56](https://github.com/saksim/vibe_job_radar/issues/56) | OPEN | feat(P1)[D10]: 51job 访问契约澄清、专用适配与独立实站验收 |
| [#57](https://github.com/saksim/vibe_job_radar/issues/57) | OPEN | docs/test(P1)[D11]: 多站能力矩阵、兼容回滚与旧 Issue 范围收口 |
| [#63](https://github.com/saksim/vibe_job_radar/issues/63) | OPEN | fix(P0): 猎聘采集主页面跳转空白，正常取数路径待实站确认 |
| [#73](https://github.com/saksim/vibe_job_radar/issues/73) | OPEN | fix: 限频等待时关闭采集浏览器，到期不应自动重开窗口 |
| [#79](https://github.com/saksim/vibe_job_radar/issues/79) | OPEN | feat: 主文档临时失败的有限重试、退避与持久预算 |
| [#93](https://github.com/saksim/vibe_job_radar/issues/93) | OPEN | fix(acquisition): 浏览器采集会话跨实例所有权与误停修复 |
| [#130](https://github.com/saksim/vibe_job_radar/issues/130) | OPEN | fix: 正常登录跳转中的DOM观察误停自动接续，保留首次CI未知条件 |
| [#139](https://github.com/saksim/vibe_job_radar/issues/139) | OPEN | fix: 原生弹窗关闭与请求拒绝回调竞争，已关闭页异常打断采集状态 |
| [#140](https://github.com/saksim/vibe_job_radar/issues/140) | OPEN | feat(G2): 持久保留逐次正文采集结果、重试与实测耗时 |
| [#144](https://github.com/saksim/vibe_job_radar/issues/144) | OPEN | fix(P0): 猎聘公开详情采集支持直接粘贴已知分享链接 |
| [#146](https://github.com/saksim/vibe_job_radar/issues/146) | OPEN | feat: 猎聘公开架构师分类自动发现与前5个完整JD采集 |
| [#148](https://github.com/saksim/vibe_job_radar/issues/148) | OPEN | fix: 猎聘a类公开详情的身份校验、缓存与自动分类支持 |
| [#150](https://github.com/saksim/vibe_job_radar/issues/150) | OPEN | feat: 公开分类名单的下一批采集与可恢复进度 |
| [#152](https://github.com/saksim/vibe_job_radar/issues/152) | OPEN | fix: 猎聘正文语义标记误拒绝（任职资格与公司信息部） |
| [#154](https://github.com/saksim/vibe_job_radar/issues/154) | OPEN | feat: 首页直接进入猎聘架构师公开分类采集 |
| [#158](https://github.com/saksim/vibe_job_radar/issues/158) | OPEN | feat: 显式接入猎聘算法工程师公开分类并保留原角色筛选 |
| [#160](https://github.com/saksim/vibe_job_radar/issues/160) | OPEN | fix: 猎聘无小标题的连续编号任职要求被误拒绝 |
| [#166](https://github.com/saksim/vibe_job_radar/issues/166) | OPEN | feat(P0): 沿已观察链接采集猎聘公开分类相邻页 |
| [#170](https://github.com/saksim/vibe_job_radar/issues/170) | OPEN | feat(P0): 从公开分类原名单恢复因限额等待中止的正文 |
| [#172](https://github.com/saksim/vibe_job_radar/issues/172) | OPEN | feat(P0): 公开分类小批在本机后台完成，关闭网页保留执行 |
| [#184](https://github.com/saksim/vibe_job_radar/issues/184) | OPEN | feat(P0): 保存原失败后显式重试已确认的公开分类页 |
| [#197](https://github.com/saksim/vibe_job_radar/issues/197) | OPEN | 浏览器进度回调覆盖旧检查点，丢失当前选择和登录接续 |
| [#208](https://github.com/saksim/vibe_job_radar/issues/208) | OPEN | fix: 猎聘分节职责和技能正文因没有通用小标题被误拒绝 |

### 公开来源与长期任务

| Issue | GitHub状态 | 历史标题 |
|---|---|---|
| [#30](https://github.com/saksim/vibe_job_radar/issues/30) | OPEN | feat: 本地优先混合数据——公开查询、来源审计、目录变化与任务恢复 |
| [#68](https://github.com/saksim/vibe_job_radar/issues/68) | OPEN | feat: 公开任务支持停止和重启后的确认继续，保留查询与已存结果 |
| [#87](https://github.com/saksim/vibe_job_radar/issues/87) | OPEN | feat(runtime): 本机公开查询持久24小时计划与异常暂停 |
| [#89](https://github.com/saksim/vibe_job_radar/issues/89) | OPEN | fix(runtime): 公开任务跨实例所有权与恢复竞态 |
| [#95](https://github.com/saksim/vibe_job_radar/issues/95) | OPEN | feat(data): 增加固定 Cloudflare 公开目录并保持来源隔离与共享配额 |
| [#101](https://github.com/saksim/vibe_job_radar/issues/101) | OPEN | feat(P1): 固定公开目录按ETag确认未变化并保留真实采集时间 |
| [#102](https://github.com/saksim/vibe_job_radar/issues/102) | OPEN | fix(P1): 防止同进程工作台状态锁竞争导致观察者等待超时 |
| [#105](https://github.com/saksim/vibe_job_radar/issues/105) | OPEN | feat(R3): Windows便携工作台按用户明确选择在登录后启动 |
| [#115](https://github.com/saksim/vibe_job_radar/issues/115) | OPEN | feat(R3): 已确认公开计划的独立本机后台工作进程 |
| [#118](https://github.com/saksim/vibe_job_radar/issues/118) | OPEN | feat(data): 接入固定 Cursor 公开目录并复用原报告与计划 |
| [#120](https://github.com/saksim/vibe_job_radar/issues/120) | OPEN | feat(R3): Windows登录后按明确选择运行独立公开计划进程 |
| [#124](https://github.com/saksim/vibe_job_radar/issues/124) | OPEN | fix(R3): 已完成计划结果被后续手动查询替换后误判中断 |
| [#131](https://github.com/saksim/vibe_job_radar/issues/131) | OPEN | feat(R3/H4): 保存有限公开查询待办，原worker按顺序执行并跨重启保留 |

### 要求提取与分析质量

| Issue | GitHub状态 | 历史标题 |
|---|---|---|
| [#107](https://github.com/saksim/vibe_job_radar/issues/107) | OPEN | fix(data): 英文公司介绍与相邻正负条款被误算为岗位要求 |
| [#164](https://github.com/saksim/vibe_job_radar/issues/164) | OPEN | fix: 编号职责软换行截断引用并丢失AI编程语境 |
| [#177](https://github.com/saksim/vibe_job_radar/issues/177) | OPEN | fix: 明确使用AI进行代码生成和评审的长句被漏记 |
| [#181](https://github.com/saksim/vibe_job_radar/issues/181) | OPEN | fix: 利用大语言模型做代码审查的明确要求漏识别 |

### 工程资格与交付

| Issue | GitHub状态 | 历史标题 |
|---|---|---|
| [#28](https://github.com/saksim/vibe_job_radar/issues/28) | OPEN | ops(P0): Actions任务无执行步骤——独立恢复远端发布资格，禁止把本地通过当CI通过 |
| [#91](https://github.com/saksim/vibe_job_radar/issues/91) | OPEN | feat(install): Windows x64 自带运行环境的便携候选包 |
| [#97](https://github.com/saksim/vibe_job_radar/issues/97) | OPEN | test(P1): 组合验证当前18项改进与可运行Windows候选 |
| [#111](https://github.com/saksim/vibe_job_radar/issues/111) | OPEN | test(P1): 原生浏览器 opening 超时保留阶段与线程栈证据 |
| [#113](https://github.com/saksim/vibe_job_radar/issues/113) | OPEN | integration: 汇总25项改进的统一源码和Windows便携候选 |
| [#117](https://github.com/saksim/vibe_job_radar/issues/117) | OPEN | bug(P1): 冻结Windows程序空白页检查超时，保留具体阶段继续定位 |
| [#122](https://github.com/saksim/vibe_job_radar/issues/122) | OPEN | integration: 汇总28项改进与自动取数运行入口的统一候选 |
| [#126](https://github.com/saksim/vibe_job_radar/issues/126) | OPEN | test: Windows登录详情准备超时后残留所有权锁，补齐清理和定位证据 |
| [#128](https://github.com/saksim/vibe_job_radar/issues/128) | OPEN | delivery: 统一30项候选，纳入计划结果恢复与失败清理验证 |
| [#133](https://github.com/saksim/vibe_job_radar/issues/133) | OPEN | chore: 整合31项改进为含持久查询待办的单一候选 |
| [#137](https://github.com/saksim/vibe_job_radar/issues/137) | OPEN | delivery: 纳入系统 PAC 的32项统一候选与独立验收 |
| [#142](https://github.com/saksim/vibe_job_radar/issues/142) | OPEN | delivery: 纳入逐次采集记录的33项统一候选与独立验收 |
| [#156](https://github.com/saksim/vibe_job_radar/issues/156) | OPEN | test: 补齐实际便携程序系统PAC失败的最小定位证据 |
| [#162](https://github.com/saksim/vibe_job_radar/issues/162) | OPEN | delivery: 汇总42项改进与失败瞬间线程诊断的当前候选 |
| [#174](https://github.com/saksim/vibe_job_radar/issues/174) | OPEN | test: Windows原生TLS回归中队列等待超时，补齐失败瞬间线程诊断 |
| [#175](https://github.com/saksim/vibe_job_radar/issues/175) | OPEN | delivery: 汇总47项改进与当前真实取数结果的统一候选 |
| [#179](https://github.com/saksim/vibe_job_radar/issues/179) | OPEN | test: Windows程序资格触及600秒总限时，保留明确超时结果 |
| [#183](https://github.com/saksim/vibe_job_radar/issues/183) | OPEN | bug: Windows冻结程序缓存worker验收出现额外PAC来源请求 |
| [#188](https://github.com/saksim/vibe_job_radar/issues/188) | OPEN | delivery: 汇总52项改进、89条真实正文及多站实际卡点的统一候选 |
| [#190](https://github.com/saksim/vibe_job_radar/issues/190) | OPEN | fix: 已初始化职位库重复写版本号，阻塞并发报告读取 |
| [#192](https://github.com/saksim/vibe_job_radar/issues/192) | OPEN | fix: 保留自动续采测试失败的源码位置，避免长断言覆盖诊断 |
| [#199](https://github.com/saksim/vibe_job_radar/issues/199) | OPEN | ci: 浏览器实测与源码资格共用10分钟作业预算导致中途取消 |
| [#202](https://github.com/saksim/vibe_job_radar/issues/202) | OPEN | test: 原生组件清理失败缺少分项事实，保留实际exe失败继续定位 |

## 全部105个PR索引

105个PR：27个已合并、2个关闭未合并、76个开放（54 Ready/22 Draft）。开放PR的头提交全部是PR209祖先；其中75个也是当前运行PR207的祖先。仅表示代码包含关系，不自动传递旧CI或实站验收资格。

| PR | GitHub状态 | 标题 | base | 已在运行207中 |
|---|---|---|---|---|
| [#2](https://github.com/saksim/vibe_job_radar/pull/2) | MERGED | feat: 本地浏览器工作台、真实 JD 使用闭环与 Windows 验收修复 | main | 历史已合并 |
| [#6](https://github.com/saksim/vibe_job_radar/pull/6) | MERGED | feat: 多平台自动采集与证据全界面化闭环 | main | 历史已合并 |
| [#8](https://github.com/saksim/vibe_job_radar/pull/8) | MERGED | docs: 零基础上手、采集能力说明与逐站爬虫路线 | main | 历史已合并 |
| [#11](https://github.com/saksim/vibe_job_radar/pull/11) | MERGED | ux: 采集字段引导、零基础文档与 main 收口修复 | main | 历史已合并 |
| [#13](https://github.com/saksim/vibe_job_radar/pull/13) | MERGED | fix: 修复一次性分支清理工作流并补充配置验证 | main | 历史已合并 |
| [#15](https://github.com/saksim/vibe_job_radar/pull/15) | MERGED | feat: 0.2.0 引导式浏览器找岗位、人工登录接管、列表到正文与共享限频 | main | 历史已合并 |
| [#18](https://github.com/saksim/vibe_job_radar/pull/18) | MERGED | fix: 浏览器安装诊断与真实启动验证，修复 Chromium 误装和笼统未就绪提示 | main | 历史已合并 |
| [#21](https://github.com/saksim/vibe_job_radar/pull/21) | MERGED | fix: 受控HTTP重定向与失败解释，提供免Key真实公开岗位启动案例 | main | 历史已合并 |
| [#24](https://github.com/saksim/vibe_job_radar/pull/24) | MERGED | feat: 合并已完成的本机 HTTP 代理与多地址容错；其余网络兼容继续跟踪 | main | 历史已合并 |
| [#26](https://github.com/saksim/vibe_job_radar/pull/26) | CLOSED / Draft | feat: 本机匿名 SOCKS5 实际连接与共享 TLS；第二阶段网络兼容 | main | 历史关闭未合并 |
| [#29](https://github.com/saksim/vibe_job_radar/pull/29) | CLOSED / Draft | build: 0.2.1源码绑定验收与开放需求归因；采集主阻断仍保持开放 | main | 历史关闭未合并 |
| [#31](https://github.com/saksim/vibe_job_radar/pull/31) | MERGED | feat: 本地优先混合核心——公开岗位查询、限频与失败续接；修复 Windows CI | main | 历史已合并 |
| [#32](https://github.com/saksim/vibe_job_radar/pull/32) | MERGED | feat: 从合并后的 main 整合 SOCKS5 自动策略，修复 NO_PROXY 地址误匹配 | main | 历史已合并 |
| [#33](https://github.com/saksim/vibe_job_radar/pull/33) | MERGED | feat: Fake-IP 本机加密解析；修复撤销、过期地址与缓存掩盖失败 | main | 历史已合并 |
| [#34](https://github.com/saksim/vibe_job_radar/pull/34) | MERGED | build: 从最新 main 整合源码绑定验收与候选打包，保留全部采集能力 | main | 历史已合并 |
| [#35](https://github.com/saksim/vibe_job_radar/pull/35) | MERGED | feat: 本地公开目录变化审计——新增、修改、未出现与报告联动 | main | 历史已合并 |
| [#36](https://github.com/saksim/vibe_job_radar/pull/36) | MERGED | feat: 回归产品主线——目标岗位研究、可读结论与同批个人证据续接 | main | 历史已合并 |
| [#37](https://github.com/saksim/vibe_job_radar/pull/37) | MERGED | fix: 主线采集登录后续接——保留会话、原任务与正文，拒绝列表误判 | main | 历史已合并 |
| [#38](https://github.com/saksim/vibe_job_radar/pull/38) | MERGED | fix: 主线目标岗位取数——补齐猎聘详情链接、匹配正文身份并保留失败结果 | main | 历史已合并 |
| [#39](https://github.com/saksim/vibe_job_radar/pull/39) | MERGED | fix: 本机启动与 Fake-IP 实际阻断——原生崩溃识别、真实重下载及有效解析诊断 | main | 历史已合并 |
| [#40](https://github.com/saksim/vibe_job_radar/pull/40) | MERGED | fix: 停止浏览器重装循环——区分就绪与操作记录，明确选择本机 Edge | main | 历史已合并 |
| [#41](https://github.com/saksim/vibe_job_radar/pull/41) | MERGED | fix: 保留加密 DNS 的具体 TLS 失败证据，解释客户端主动中止而非平台封禁 | main | 历史已合并 |
| [#42](https://github.com/saksim/vibe_job_radar/pull/42) | MERGED | fix: Windows verify_code 20——原生证书链验证与无需重装浏览器的一次修复 | main | 历史已合并 |
| [#43](https://github.com/saksim/vibe_job_radar/pull/43) | MERGED | fix: 网络检查成功但采集仍拒绝 Fake-IP——在浏览器回调前绑定工作区策略 | main | 历史已合并 |
| [#44](https://github.com/saksim/vibe_job_radar/pull/44) | MERGED | fix: 采集请求头去重与真实动态页面验收，分流三站 robots 阻断 | main | 历史已合并 |
| [#58](https://github.com/saksim/vibe_job_radar/pull/58) | MERGED | feat(D01): 本地脱敏取数诊断、失败分层与原生采集访问契约 | main | 历史已合并 |
| [#59](https://github.com/saksim/vibe_job_radar/pull/59) | MERGED | feat(D02): 原生浏览器网络、逐跳业务控制与受控验收 | main | 历史已合并 |
| [#60](https://github.com/saksim/vibe_job_radar/pull/60) | MERGED | feat(D03): 登录后自动接续、跨查询会话复用与完整岗位正文到报告 | main | 历史已合并 |
| [#61](https://github.com/saksim/vibe_job_radar/pull/61) | MERGED | feat(D04): 重启后恢复本机 Cookie 会话并继续原有岗位采集 | main | 历史已合并 |
| [#62](https://github.com/saksim/vibe_job_radar/pull/62) | OPEN / Ready | feat(liepin): 正常登录接续、小批取数、检查点与逐条结果 | main | 是（头提交祖先） |
| [#66](https://github.com/saksim/vibe_job_radar/pull/66) | OPEN / Ready | fix(N3): 高级采集与 CLI 统一使用工作区网络偏好 | main | 是（头提交祖先） |
| [#67](https://github.com/saksim/vibe_job_radar/pull/67) | OPEN / Ready | fix: Windows 拒绝 POST 后有界消费正文，可靠返回权限错误 | main | 是（头提交祖先） |
| [#69](https://github.com/saksim/vibe_job_radar/pull/69) | OPEN / Ready | feat(public): 公开任务停止与重启后确认继续，保留缓存和报告 | main | 是（头提交祖先） |
| [#70](https://github.com/saksim/vibe_job_radar/pull/70) | OPEN / Ready | feat(D11): 统一采集能力状态并实测工作区升级与回退 | main | 是（头提交祖先） |
| [#71](https://github.com/saksim/vibe_job_radar/pull/71) | OPEN / Ready | feat(acceptance): 预登记范围并离线核对整批取数证据 | main | 是（头提交祖先） |
| [#72](https://github.com/saksim/vibe_job_radar/pull/72) | OPEN / Draft | test: 本轮采集与恢复改动的集成验收 | main | 是（头提交祖先） |
| [#74](https://github.com/saksim/vibe_job_radar/pull/74) | OPEN / Ready | fix: 将限频自动恢复绑定到仍有效的原采集会话 | main | 是（头提交祖先） |
| [#76](https://github.com/saksim/vibe_job_radar/pull/76) | OPEN / Ready | feat: 支持显式本机 HTTP/SOCKS5 代理认证并隔离凭据 | main | 是（头提交祖先） |
| [#78](https://github.com/saksim/vibe_job_radar/pull/78) | OPEN / Ready | fix: 等向导初始化就绪后启用表单，避免吞掉首次操作 | main | 是（头提交祖先） |
| [#80](https://github.com/saksim/vibe_job_radar/pull/80) | OPEN / Ready | feat: 主文档临时失败有限重试，保留配额、会话及已采集结果 | codex/deferred-browser-lifecycle | 是（头提交祖先） |
| [#82](https://github.com/saksim/vibe_job_radar/pull/82) | OPEN / Ready | feat: 工作区网页代理设置，统一采集和CLI策略 | codex/integration-current | 是（头提交祖先） |
| [#84](https://github.com/saksim/vibe_job_radar/pull/84) | OPEN / Ready | feat(network): 显式宿主机代理与两端隔离网络验证 | codex/workspace-proxy-settings | 是（头提交祖先） |
| [#86](https://github.com/saksim/vibe_job_radar/pull/86) | OPEN / Ready | fix(acquisition): 统一HTTP与浏览器的robots规则决定 | codex/vm-host-proxy | 是（头提交祖先） |
| [#88](https://github.com/saksim/vibe_job_radar/pull/88) | OPEN / Ready | feat(runtime): 本机公开查询24小时计划与异常暂停 | codex/shared-robots | 是（头提交祖先） |
| [#90](https://github.com/saksim/vibe_job_radar/pull/90) | OPEN / Ready | fix(runtime): 同工作区公开任务所有权与安全恢复 | codex/public-schedule | 是（头提交祖先） |
| [#92](https://github.com/saksim/vibe_job_radar/pull/92) | OPEN / Ready | feat(install): Windows x64 自带运行环境的便携候选 | codex/public-task-owner | 是（头提交祖先） |
| [#94](https://github.com/saksim/vibe_job_radar/pull/94) | OPEN / Ready | fix(acquisition): 浏览器会话跨工作台所有权与误停修复 | codex/public-task-owner | 是（头提交祖先） |
| [#96](https://github.com/saksim/vibe_job_radar/pull/96) | OPEN / Ready | feat(data): 接入Cloudflare真实公开目录、隔离缓存并优先匹配岗位标题 | codex/guided-task-owner | 是（头提交祖先） |
| [#98](https://github.com/saksim/vibe_job_radar/pull/98) | OPEN / Draft | test: 整合18项采集改进、真实公开数据和Windows便携候选 | main | 是（头提交祖先） |
| [#100](https://github.com/saksim/vibe_job_radar/pull/100) | OPEN / Ready | fix: 统一登录能力说明并纳入状态读取公平修复 | codex/current-delivery | 是（头提交祖先） |
| [#103](https://github.com/saksim/vibe_job_radar/pull/103) | OPEN / Ready | fix(runtime): 公平排队公开任务状态读写，避免本机观察者饥饿 | codex/public-task-owner | 是（头提交祖先） |
| [#104](https://github.com/saksim/vibe_job_radar/pull/104) | OPEN / Ready | feat(data): 用服务器版本确认目录未变并保留真实采集时间 | codex/login-status-ui | 是（头提交祖先） |
| [#106](https://github.com/saksim/vibe_job_radar/pull/106) | OPEN / Ready | feat: Windows便携工作台支持明确选择的登录启动 | codex/public-revalidation | 是（头提交祖先） |
| [#108](https://github.com/saksim/vibe_job_radar/pull/108) | OPEN / Ready | fix: 区分英文岗位要求与公司产品提及 | codex/windows-startup | 是（头提交祖先） |
| [#110](https://github.com/saksim/vibe_job_radar/pull/110) | OPEN / Ready | feat(N6): 显式导入 Windows 域名 PAC 并保留原始选路 | codex/english-obligation | 是（头提交祖先） |
| [#112](https://github.com/saksim/vibe_job_radar/pull/112) | OPEN / Ready | test: 保留原生 opening 超时阶段与线程栈 | codex/windows-pac | 是（头提交祖先） |
| [#114](https://github.com/saksim/vibe_job_radar/pull/114) | OPEN / Draft | integration: 25项采集与运行改进的统一交付候选 | main | 是（头提交祖先） |
| [#116](https://github.com/saksim/vibe_job_radar/pull/116) | OPEN / Ready | feat: 已确认公开计划可在独立后台进程执行 | codex/delivery-all-current | 是（头提交祖先） |
| [#119](https://github.com/saksim/vibe_job_radar/pull/119) | OPEN / Ready | feat: 接入 Cursor 官方公开目录、原报告及每日计划 | codex/public-worker | 是（头提交祖先） |
| [#121](https://github.com/saksim/vibe_job_radar/pull/121) | OPEN / Ready | feat: Windows登录时明确选择独立公开计划进程 | codex/cursor-public | 是（头提交祖先） |
| [#123](https://github.com/saksim/vibe_job_radar/pull/123) | OPEN / Draft | integration: 统一28项采集与自动运行改进及Windows候选 | main | 是（头提交祖先） |
| [#125](https://github.com/saksim/vibe_job_radar/pull/125) | OPEN / Ready | fix(R3): 保留计划结果回执，避免后续手动查询误暂停计划 | codex/delivery-28 | 是（头提交祖先） |
| [#127](https://github.com/saksim/vibe_job_radar/pull/127) | OPEN / Ready | fix: 保留正常登录跳转后的接续，修复失败清理并保存阶段证据 | codex/delivery-28 | 是（头提交祖先） |
| [#129](https://github.com/saksim/vibe_job_radar/pull/129) | OPEN / Draft | delivery: 统一30项取数与运行改进，保留当前验证和未知卡点 | main | 是（头提交祖先） |
| [#132](https://github.com/saksim/vibe_job_radar/pull/132) | OPEN / Ready | feat: 保存有限公开查询待办，由工作台或独立进程顺序执行 | codex/delivery-30 | 是（头提交祖先） |
| [#134](https://github.com/saksim/vibe_job_radar/pull/134) | OPEN / Draft | chore: 提供含持久查询待办的31项改进统一候选 | main | 是（头提交祖先） |
| [#136](https://github.com/saksim/vibe_job_radar/pull/136) | OPEN / Ready | feat: 确认后自动使用 Windows 已配置 PAC | codex/delivery-31 | 是（头提交祖先） |
| [#138](https://github.com/saksim/vibe_job_radar/pull/138) | OPEN / Draft | delivery: 整合系统 PAC 的32项统一候选 | main | 是（头提交祖先） |
| [#141](https://github.com/saksim/vibe_job_radar/pull/141) | OPEN / Ready | feat: 保留逐次正文采集历史与实测处理耗时 | codex/delivery-32 | 是（头提交祖先） |
| [#143](https://github.com/saksim/vibe_job_radar/pull/143) | OPEN / Draft | delivery: 整合逐次正文记录的33项统一候选 | main | 是（头提交祖先） |
| [#145](https://github.com/saksim/vibe_job_radar/pull/145) | OPEN / Ready | fix: 猎聘公开职位链接采集、身份校验与完整 JD 报告 | codex/delivery-33 | 是（头提交祖先） |
| [#147](https://github.com/saksim/vibe_job_radar/pull/147) | OPEN / Ready | feat: 猎聘架构师公开分类自动采集与完整JD报告 | codex/liepin-public-links | 是（头提交祖先） |
| [#149](https://github.com/saksim/vibe_job_radar/pull/149) | OPEN / Ready | fix: 猎聘a类详情严格校验与分类采集支持 | codex/liepin-public-category | 是（头提交祖先） |
| [#151](https://github.com/saksim/vibe_job_radar/pull/151) | OPEN / Ready | feat: 按保存名单继续猎聘分类下一批（#150） | codex/liepin-a-details | 是（头提交祖先） |
| [#153](https://github.com/saksim/vibe_job_radar/pull/153) | OPEN / Draft | fix: 修复猎聘正文标题与公司信息部词组误拒绝（#152） | codex/category-next-batch | 是（头提交祖先） |
| [#155](https://github.com/saksim/vibe_job_radar/pull/155) | OPEN / Ready | feat: 首页直达猎聘公开分类采集（#154） | codex/liepin-intro-labels | 是（头提交祖先） |
| [#157](https://github.com/saksim/vibe_job_radar/pull/157) | OPEN / Ready | test: 保留实际便携程序系统PAC失败定位（#156） | codex/liepin-category-home | 是（头提交祖先） |
| [#159](https://github.com/saksim/vibe_job_radar/pull/159) | OPEN / Ready | feat: 猎聘算法工程师分类自动采集与原目标岗位筛选 | codex/portable-failure-diagnostics | 是（头提交祖先） |
| [#161](https://github.com/saksim/vibe_job_radar/pull/161) | OPEN / Ready | fix: 猎聘完整编号任职要求的严格解析误拒绝 | codex/liepin-algorithm-category | 是（头提交祖先） |
| [#163](https://github.com/saksim/vibe_job_radar/pull/163) | OPEN / Draft | delivery: integrate 42 improvements and preserve test failure stacks | main | 是（头提交祖先） |
| [#165](https://github.com/saksim/vibe_job_radar/pull/165) | OPEN / Ready | fix: 保留编号中文职责的完整续行与原文证据 | codex/delivery-42 | 是（头提交祖先） |
| [#167](https://github.com/saksim/vibe_job_radar/pull/167) | OPEN / Ready | feat: 沿已观察链接采集猎聘公开分类相邻页 | codex/numbered-item-wraps | 是（头提交祖先） |
| [#169](https://github.com/saksim/vibe_job_radar/pull/169) | OPEN / Ready | fix: 高级HTTP与浏览器共用持久限额和冷却 | codex/public-category-pages | 是（头提交祖先） |
| [#171](https://github.com/saksim/vibe_job_radar/pull/171) | OPEN / Ready | feat: 从原分类名单恢复因等待未完成的正文 | codex/advanced-shared-rate | 是（头提交祖先） |
| [#173](https://github.com/saksim/vibe_job_radar/pull/173) | OPEN / Ready | feat: 关闭网页后由本机后台完成当前分类批次 | codex/category-rate-recovery | 是（头提交祖先） |
| [#176](https://github.com/saksim/vibe_job_radar/pull/176) | OPEN / Draft | delivery: 统一47项采集改进与真实84正文的候选版本 | main | 是（头提交祖先） |
| [#178](https://github.com/saksim/vibe_job_radar/pull/178) | OPEN / Ready | fix: 识别有明确使用语境的AI编程长句 | codex/delivery-47 | 是（头提交祖先） |
| [#180](https://github.com/saksim/vibe_job_radar/pull/180) | OPEN / Ready | fix: 保留程序资格超时的步骤与时限 | codex/explicit-ai-application | 是（头提交祖先） |
| [#182](https://github.com/saksim/vibe_job_radar/pull/182) | OPEN / Ready | fix: 识别明确使用模型审查代码的要求 | codex/candidate-timeout-evidence | 是（头提交祖先） |
| [#185](https://github.com/saksim/vibe_job_radar/pull/185) | OPEN / Ready | feat: 分类页网络失败后保存有界同页重试 | codex/model-code-review | 是（头提交祖先） |
| [#187](https://github.com/saksim/vibe_job_radar/pull/187) | OPEN / Ready | fix: honor leading robots wildcards across collection backends | codex/category-page-retry | 是（头提交祖先） |
| [#189](https://github.com/saksim/vibe_job_radar/pull/189) | OPEN / Draft | delivery: 统一52项改进、89条正文与多站卡点 | main | 是（头提交祖先） |
| [#191](https://github.com/saksim/vibe_job_radar/pull/191) | OPEN / Ready | fix: 已初始化职位库避免重复版本写锁，保持并发报告读取 | codex/delivery-52 | 是（头提交祖先） |
| [#193](https://github.com/saksim/vibe_job_radar/pull/193) | OPEN / Ready | fix: 保留回归失败的源码位置，避免长断言遮住诊断 | codex/store-read-open | 是（头提交祖先） |
| [#195](https://github.com/saksim/vibe_job_radar/pull/195) | OPEN / Ready | docs: 对齐首次取数、登录会话与后台执行指南 | codex/test-failure-location | 是（头提交祖先） |
| [#196](https://github.com/saksim/vibe_job_radar/pull/196) | OPEN / Draft | fix: stabilize native page control and durable reports | codex/collection-guide-current-paths | 是（头提交祖先） |
| [#198](https://github.com/saksim/vibe_job_radar/pull/198) | OPEN / Draft | test: retain public owner wait evidence before child cleanup | codex/native-cdp-events | 是（头提交祖先） |
| [#200](https://github.com/saksim/vibe_job_radar/pull/200) | OPEN / Draft | ci: split browser and source qualification job budgets | codex/public-owner-wait-diagnostics | 是（头提交祖先） |
| [#201](https://github.com/saksim/vibe_job_radar/pull/201) | OPEN / Draft | docs: align acquisition status with actual scoped observations | codex/browser-source-job-budgets | 是（头提交祖先） |
| [#203](https://github.com/saksim/vibe_job_radar/pull/203) | OPEN / Draft | test: preserve native cleanup completion facts in actual executable evidence | codex/current-acquisition-evidence | 是（头提交祖先） |
| [#204](https://github.com/saksim/vibe_job_radar/pull/204) | OPEN / Draft | fix: 猎聘未勾选条款时明确交接并保留首次停止诊断 | codex/native-cleanup-facts | 是（头提交祖先） |
| [#205](https://github.com/saksim/vibe_job_radar/pull/205) | OPEN / Draft | feat: 本机 Chrome 采集选择与独立原生启动检查 | codex/liepin-login-agreement | 是（头提交祖先） |
| [#206](https://github.com/saksim/vibe_job_radar/pull/206) | OPEN / Draft | 修复猎聘默认短信页未切换到密码登录 | codex/chrome-login-choice | 是（头提交祖先） |
| [#207](https://github.com/saksim/vibe_job_radar/pull/207) | OPEN / Draft | fix: 登录前显示实际次数限制和恢复时间 | codex/liepin-password-tab | 是（头提交祖先） |
| [#209](https://github.com/saksim/vibe_job_radar/pull/209) | OPEN / Draft | fix: 识别无通用小标题的分节职责与资格正文 | codex/login-quota-status | 否，独立待验修复 |
