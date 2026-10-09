# 2026-09-27开放PR索引历史

以下完整保留2026年9月27日固定PR220/67a765ca的交接材料。81个开放PR、旧main、221个提交、378个路径、535份源码与旧包摘要均属于该快照，不是当前状态；旧工件记录的2026年10月4日到期日已过去，原链接不作为当前分发入口。历史包验证与本PR自己的源码、CI和实际程序资格分别记录，当前提交的结果见[原PR #222](https://github.com/saksim/vibe_job_radar/pull/222)及[交付清单](DELIVERY_STATUS.md)。

用户后续已授权核验后逐项合并并要求收敛现有PR，因此下方另建集成PR、等待合并决定等旧建议不作为当前执行计划。旧候选初始失败、修正后的首轮结果，以及原交接提交自己的首轮22/23与一次历史复核均保持，后续通过不替代根因调查。普通使用与真实取数的剩余条件见[当前证据](CURRENT_ACQUISITION_EVIDENCE.md)。本轮仅整合原交接范围，不更新用户工作台、下载过期旧包或发布Release；升级/回退仍按准确源码、格式与完整私有备份核对。

---

# PR220 固定候选的开放 PR 审查索引

核查于 2026-09-27T05:41:35.544042+00:00，候选 `67a765cac1eda9bdabf44e5ce65f5a8ab27df742`；对应[交接清单](MAIN_HANDOFF_REVIEW.md)与[机器证据](evidence/pr220-handoff.json)。此后新增的交接PR不计入冻结的81项，状态不是自动更新。

全部头提交已包含在候选历史中，均仍为开放PR。Ready仅表示非Draft。审查列原样保留API的reviewDecision；空值表示未报告，不自行推断批准或拒绝。基底是核查时的分支头，不是假称创建PR时的历史基底。基底关联的PR由开放PR的head分支名匹配，仍须按Git祖先列核对。

| PR | 变更标题 | 状态 / 审查 | 头提交 | 当前基底 / 对应开放PR | 基底是该头的祖先 |
|---|---|---|---|---|---|
| [#62](https://github.com/saksim/vibe_job_radar/pull/62) | feat(liepin): 正常登录接续、小批取数、检查点与逐条结果 | Ready / 未报告 | `0d85f5548187` | `main` / 无对应开放PR | 是 |
| [#66](https://github.com/saksim/vibe_job_radar/pull/66) | fix(N3): 高级采集与 CLI 统一使用工作区网络偏好 | Ready / 未报告 | `75d8cf5b9f7d` | `main` / 无对应开放PR | 是 |
| [#67](https://github.com/saksim/vibe_job_radar/pull/67) | fix: Windows 拒绝 POST 后有界消费正文，可靠返回权限错误 | Ready / 未报告 | `25cac6e2d056` | `main` / 无对应开放PR | 是 |
| [#69](https://github.com/saksim/vibe_job_radar/pull/69) | feat(public): 公开任务停止与重启后确认继续，保留缓存和报告 | Ready / 未报告 | `0ad335d174ff` | `main` / 无对应开放PR | 是 |
| [#70](https://github.com/saksim/vibe_job_radar/pull/70) | feat(D11): 统一采集能力状态并实测工作区升级与回退 | Ready / 未报告 | `28922011f54f` | `main` / 无对应开放PR | 是 |
| [#71](https://github.com/saksim/vibe_job_radar/pull/71) | feat(acceptance): 预登记范围并离线核对整批取数证据 | Ready / 未报告 | `f23abbda1f0a` | `main` / 无对应开放PR | 是 |
| [#72](https://github.com/saksim/vibe_job_radar/pull/72) | test: 本轮采集与恢复改动的集成验收 | Draft / 未报告 | `7cfbbd791557` | `main` / 无对应开放PR | 是 |
| [#74](https://github.com/saksim/vibe_job_radar/pull/74) | fix: 将限频自动恢复绑定到仍有效的原采集会话 | Ready / 未报告 | `9fb54b7887a2` | `main` / 无对应开放PR | 是 |
| [#76](https://github.com/saksim/vibe_job_radar/pull/76) | feat: 支持显式本机 HTTP/SOCKS5 代理认证并隔离凭据 | Ready / 未报告 | `374edd1d0a13` | `main` / 无对应开放PR | 是 |
| [#78](https://github.com/saksim/vibe_job_radar/pull/78) | fix: 等向导初始化就绪后启用表单，避免吞掉首次操作 | Ready / 未报告 | `e62e69c00499` | `main` / 无对应开放PR | 是 |
| [#80](https://github.com/saksim/vibe_job_radar/pull/80) | feat: 主文档临时失败有限重试，保留配额、会话及已采集结果 | Ready / 未报告 | `adfd7865a694` | `codex/deferred-browser-lifecycle` / #74 | 是 |
| [#82](https://github.com/saksim/vibe_job_radar/pull/82) | feat: 工作区网页代理设置，统一采集和CLI策略 | Ready / 未报告 | `de18de8f2329` | `codex/integration-current` / #72 | 是 |
| [#84](https://github.com/saksim/vibe_job_radar/pull/84) | feat(network): 显式宿主机代理与两端隔离网络验证 | Ready / 未报告 | `c656b8a8357b` | `codex/workspace-proxy-settings` / #82 | 是 |
| [#86](https://github.com/saksim/vibe_job_radar/pull/86) | fix(acquisition): 统一HTTP与浏览器的robots规则决定 | Ready / 未报告 | `41c19eae6ef0` | `codex/vm-host-proxy` / #84 | 是 |
| [#88](https://github.com/saksim/vibe_job_radar/pull/88) | feat(runtime): 本机公开查询24小时计划与异常暂停 | Ready / 未报告 | `086818a01316` | `codex/shared-robots` / #86 | 是 |
| [#90](https://github.com/saksim/vibe_job_radar/pull/90) | fix(runtime): 同工作区公开任务所有权与安全恢复 | Ready / 未报告 | `a0464341e64e` | `codex/public-schedule` / #88 | 是 |
| [#92](https://github.com/saksim/vibe_job_radar/pull/92) | feat(install): Windows x64 自带运行环境的便携候选 | Ready / 未报告 | `c80f9362fd26` | `codex/public-task-owner` / #90 | 是 |
| [#94](https://github.com/saksim/vibe_job_radar/pull/94) | fix(acquisition): 浏览器会话跨工作台所有权与误停修复 | Ready / 未报告 | `dfe8afd16f81` | `codex/public-task-owner` / #90 | 是 |
| [#96](https://github.com/saksim/vibe_job_radar/pull/96) | feat(data): 接入Cloudflare真实公开目录、隔离缓存并优先匹配岗位标题 | Ready / 未报告 | `9cd076ea93f5` | `codex/guided-task-owner` / #94 | 是 |
| [#98](https://github.com/saksim/vibe_job_radar/pull/98) | test: 整合18项采集改进、真实公开数据和Windows便携候选 | Draft / 未报告 | `c5eb434ba4f5` | `main` / 无对应开放PR | 是 |
| [#100](https://github.com/saksim/vibe_job_radar/pull/100) | fix: 统一登录能力说明并纳入状态读取公平修复 | Ready / 未报告 | `5abf65ef0f21` | `codex/current-delivery` / #98 | 是 |
| [#103](https://github.com/saksim/vibe_job_radar/pull/103) | fix(runtime): 公平排队公开任务状态读写，避免本机观察者饥饿 | Ready / 未报告 | `f7312352f11c` | `codex/public-task-owner` / #90 | 是 |
| [#104](https://github.com/saksim/vibe_job_radar/pull/104) | feat(data): 用服务器版本确认目录未变并保留真实采集时间 | Ready / 未报告 | `cf477ac0a5bd` | `codex/login-status-ui` / #100 | 是 |
| [#106](https://github.com/saksim/vibe_job_radar/pull/106) | feat: Windows便携工作台支持明确选择的登录启动 | Ready / 未报告 | `12502a50e4c2` | `codex/public-revalidation` / #104 | 是 |
| [#108](https://github.com/saksim/vibe_job_radar/pull/108) | fix: 区分英文岗位要求与公司产品提及 | Ready / 未报告 | `ede3d8750e46` | `codex/windows-startup` / #106 | 是 |
| [#110](https://github.com/saksim/vibe_job_radar/pull/110) | feat(N6): 显式导入 Windows 域名 PAC 并保留原始选路 | Ready / 未报告 | `9cb569c0ccf8` | `codex/english-obligation` / #108 | 是 |
| [#112](https://github.com/saksim/vibe_job_radar/pull/112) | test: 保留原生 opening 超时阶段与线程栈 | Ready / 未报告 | `09cd38fec1a6` | `codex/windows-pac` / #110 | 是 |
| [#114](https://github.com/saksim/vibe_job_radar/pull/114) | integration: 25项采集与运行改进的统一交付候选 | Draft / 未报告 | `ab2d8b77a4c0` | `main` / 无对应开放PR | 是 |
| [#116](https://github.com/saksim/vibe_job_radar/pull/116) | feat: 已确认公开计划可在独立后台进程执行 | Ready / 未报告 | `e45fa392ff68` | `codex/delivery-all-current` / #114 | 是 |
| [#119](https://github.com/saksim/vibe_job_radar/pull/119) | feat: 接入 Cursor 官方公开目录、原报告及每日计划 | Ready / 未报告 | `4ecd222155a4` | `codex/public-worker` / #116 | 是 |
| [#121](https://github.com/saksim/vibe_job_radar/pull/121) | feat: Windows登录时明确选择独立公开计划进程 | Ready / 未报告 | `c43cc07f6214` | `codex/cursor-public` / #119 | 是 |
| [#123](https://github.com/saksim/vibe_job_radar/pull/123) | integration: 统一28项采集与自动运行改进及Windows候选 | Draft / 未报告 | `d0a0b2eb44eb` | `main` / 无对应开放PR | 是 |
| [#125](https://github.com/saksim/vibe_job_radar/pull/125) | fix(R3): 保留计划结果回执，避免后续手动查询误暂停计划 | Ready / 未报告 | `e4963f783e08` | `codex/delivery-28` / #123 | 是 |
| [#127](https://github.com/saksim/vibe_job_radar/pull/127) | fix: 保留正常登录跳转后的接续，修复失败清理并保存阶段证据 | Ready / 未报告 | `5326664b8842` | `codex/delivery-28` / #123 | 是 |
| [#129](https://github.com/saksim/vibe_job_radar/pull/129) | delivery: 统一30项取数与运行改进，保留当前验证和未知卡点 | Draft / 未报告 | `a49fcea231ef` | `main` / 无对应开放PR | 是 |
| [#132](https://github.com/saksim/vibe_job_radar/pull/132) | feat: 保存有限公开查询待办，由工作台或独立进程顺序执行 | Ready / 未报告 | `3f5077d35d66` | `codex/delivery-30` / #129 | 是 |
| [#134](https://github.com/saksim/vibe_job_radar/pull/134) | chore: 提供含持久查询待办的31项改进统一候选 | Draft / 未报告 | `f6b62f3da687` | `main` / 无对应开放PR | 是 |
| [#136](https://github.com/saksim/vibe_job_radar/pull/136) | feat: 确认后自动使用 Windows 已配置 PAC | Ready / 未报告 | `bfcb4a745d38` | `codex/delivery-31` / #134 | 是 |
| [#138](https://github.com/saksim/vibe_job_radar/pull/138) | delivery: 整合系统 PAC 的32项统一候选 | Draft / 未报告 | `91f454d6bf3c` | `main` / 无对应开放PR | 是 |
| [#141](https://github.com/saksim/vibe_job_radar/pull/141) | feat: 保留逐次正文采集历史与实测处理耗时 | Ready / 未报告 | `352b99614f0f` | `codex/delivery-32` / #138 | 是 |
| [#143](https://github.com/saksim/vibe_job_radar/pull/143) | delivery: 整合逐次正文记录的33项统一候选 | Draft / 未报告 | `5959c2881ce7` | `main` / 无对应开放PR | 是 |
| [#145](https://github.com/saksim/vibe_job_radar/pull/145) | fix: 猎聘公开职位链接采集、身份校验与完整 JD 报告 | Ready / 未报告 | `910c2e35cb51` | `codex/delivery-33` / #143 | 是 |
| [#147](https://github.com/saksim/vibe_job_radar/pull/147) | feat: 猎聘架构师公开分类自动采集与完整JD报告 | Ready / 未报告 | `9a8cc38679df` | `codex/liepin-public-links` / #145 | 是 |
| [#149](https://github.com/saksim/vibe_job_radar/pull/149) | fix: 猎聘a类详情严格校验与分类采集支持 | Ready / 未报告 | `3bf830f6b5e1` | `codex/liepin-public-category` / #147 | 是 |
| [#151](https://github.com/saksim/vibe_job_radar/pull/151) | feat: 按保存名单继续猎聘分类下一批（#150） | Ready / 未报告 | `dc611c339531` | `codex/liepin-a-details` / #149 | 是 |
| [#153](https://github.com/saksim/vibe_job_radar/pull/153) | fix: 修复猎聘正文标题与公司信息部词组误拒绝（#152） | Draft / 未报告 | `2c2ede819140` | `codex/category-next-batch` / #151 | 是 |
| [#155](https://github.com/saksim/vibe_job_radar/pull/155) | feat: 首页直达猎聘公开分类采集（#154） | Ready / 未报告 | `d95515e99796` | `codex/liepin-intro-labels` / #153 | 是 |
| [#157](https://github.com/saksim/vibe_job_radar/pull/157) | test: 保留实际便携程序系统PAC失败定位（#156） | Ready / 未报告 | `bac9865217c7` | `codex/liepin-category-home` / #155 | 是 |
| [#159](https://github.com/saksim/vibe_job_radar/pull/159) | feat: 猎聘算法工程师分类自动采集与原目标岗位筛选 | Ready / 未报告 | `c803b6e5b9ed` | `codex/portable-failure-diagnostics` / #157 | 是 |
| [#161](https://github.com/saksim/vibe_job_radar/pull/161) | fix: 猎聘完整编号任职要求的严格解析误拒绝 | Ready / 未报告 | `af16c4214c76` | `codex/liepin-algorithm-category` / #159 | 是 |
| [#163](https://github.com/saksim/vibe_job_radar/pull/163) | delivery: integrate 42 improvements and preserve test failure stacks | Draft / 未报告 | `aa08392cd8cf` | `main` / 无对应开放PR | 是 |
| [#165](https://github.com/saksim/vibe_job_radar/pull/165) | fix: 保留编号中文职责的完整续行与原文证据 | Ready / 未报告 | `4271a632c174` | `codex/delivery-42` / #163 | 是 |
| [#167](https://github.com/saksim/vibe_job_radar/pull/167) | feat: 沿已观察链接采集猎聘公开分类相邻页 | Ready / 未报告 | `1353815c04fb` | `codex/numbered-item-wraps` / #165 | 是 |
| [#169](https://github.com/saksim/vibe_job_radar/pull/169) | fix: 高级HTTP与浏览器共用持久限额和冷却 | Ready / 未报告 | `d7c4759c42fb` | `codex/public-category-pages` / #167 | 是 |
| [#171](https://github.com/saksim/vibe_job_radar/pull/171) | feat: 从原分类名单恢复因等待未完成的正文 | Ready / 未报告 | `18d8426c16ae` | `codex/advanced-shared-rate` / #169 | 是 |
| [#173](https://github.com/saksim/vibe_job_radar/pull/173) | feat: 关闭网页后由本机后台完成当前分类批次 | Ready / 未报告 | `dd3315415aa0` | `codex/category-rate-recovery` / #171 | 是 |
| [#176](https://github.com/saksim/vibe_job_radar/pull/176) | delivery: 统一47项采集改进与真实84正文的候选版本 | Draft / 未报告 | `af60477ee383` | `main` / 无对应开放PR | 是 |
| [#178](https://github.com/saksim/vibe_job_radar/pull/178) | fix: 识别有明确使用语境的AI编程长句 | Ready / 未报告 | `c05cce4e6d3b` | `codex/delivery-47` / #176 | 是 |
| [#180](https://github.com/saksim/vibe_job_radar/pull/180) | fix: 保留程序资格超时的步骤与时限 | Ready / 未报告 | `6e9f7f27d079` | `codex/explicit-ai-application` / #178 | 是 |
| [#182](https://github.com/saksim/vibe_job_radar/pull/182) | fix: 识别明确使用模型审查代码的要求 | Ready / 未报告 | `0b0b51123f26` | `codex/candidate-timeout-evidence` / #180 | 是 |
| [#185](https://github.com/saksim/vibe_job_radar/pull/185) | feat: 分类页网络失败后保存有界同页重试 | Ready / 未报告 | `4d9d36201d91` | `codex/model-code-review` / #182 | 是 |
| [#187](https://github.com/saksim/vibe_job_radar/pull/187) | fix: honor leading robots wildcards across collection backends | Ready / 未报告 | `88a46cdf0af9` | `codex/category-page-retry` / #185 | 是 |
| [#189](https://github.com/saksim/vibe_job_radar/pull/189) | delivery: 统一52项改进、89条正文与多站卡点 | Draft / 未报告 | `f0f79473baa5` | `main` / 无对应开放PR | 是 |
| [#191](https://github.com/saksim/vibe_job_radar/pull/191) | fix: 已初始化职位库避免重复版本写锁，保持并发报告读取 | Ready / 未报告 | `8bf01170f8f6` | `codex/delivery-52` / #189 | 是 |
| [#193](https://github.com/saksim/vibe_job_radar/pull/193) | fix: 保留回归失败的源码位置，避免长断言遮住诊断 | Ready / 未报告 | `6d5fef2739a3` | `codex/store-read-open` / #191 | 是 |
| [#195](https://github.com/saksim/vibe_job_radar/pull/195) | docs: 对齐首次取数、登录会话与后台执行指南 | Ready / 未报告 | `7050bc1952f8` | `codex/test-failure-location` / #193 | 是 |
| [#196](https://github.com/saksim/vibe_job_radar/pull/196) | fix: stabilize native page control and durable reports | Draft / 未报告 | `198798667119` | `codex/collection-guide-current-paths` / #195 | 是 |
| [#198](https://github.com/saksim/vibe_job_radar/pull/198) | test: retain public owner wait evidence before child cleanup | Draft / 未报告 | `bb10e77b5adf` | `codex/native-cdp-events` / #196 | 是 |
| [#200](https://github.com/saksim/vibe_job_radar/pull/200) | ci: split browser and source qualification job budgets | Draft / 未报告 | `6174d4abc549` | `codex/public-owner-wait-diagnostics` / #198 | 是 |
| [#201](https://github.com/saksim/vibe_job_radar/pull/201) | docs: align acquisition status with actual scoped observations | Draft / 未报告 | `a22d775bc203` | `codex/browser-source-job-budgets` / #200 | 是 |
| [#203](https://github.com/saksim/vibe_job_radar/pull/203) | test: preserve native cleanup completion facts in actual executable evidence | Draft / 未报告 | `b8630cc72471` | `codex/current-acquisition-evidence` / #201 | 是 |
| [#204](https://github.com/saksim/vibe_job_radar/pull/204) | fix: 猎聘未勾选条款时明确交接并保留首次停止诊断 | Draft / 未报告 | `5d4b0e74cdd5` | `codex/native-cleanup-facts` / #203 | 是 |
| [#205](https://github.com/saksim/vibe_job_radar/pull/205) | feat: 本机 Chrome 采集选择与独立原生启动检查 | Draft / 未报告 | `a11dd52c3d13` | `codex/liepin-login-agreement` / #204 | 是 |
| [#206](https://github.com/saksim/vibe_job_radar/pull/206) | 修复猎聘默认短信页未切换到密码登录 | Draft / 未报告 | `ca924ecc9782` | `codex/chrome-login-choice` / #205 | 是 |
| [#207](https://github.com/saksim/vibe_job_radar/pull/207) | fix: 登录前显示实际次数限制和恢复时间 | Draft / 未报告 | `e3ae2dfeea8a` | `codex/liepin-password-tab` / #206 | 是 |
| [#209](https://github.com/saksim/vibe_job_radar/pull/209) | fix: 识别无通用小标题的分节职责与资格正文 | Draft / 未报告 | `5ff7781d65e2` | `codex/login-quota-status` / #207 | 是 |
| [#212](https://github.com/saksim/vibe_job_radar/pull/212) | docs: 统一当前取数证据与历史目标追溯（G08） | Ready / 未报告 | `7db2b29537b6` | `codex/liepin-labelled-intro` / #209 | 是 |
| [#214](https://github.com/saksim/vibe_job_radar/pull/214) | feat: 原研究报告显示各岗位方向真实样本分母（G02） | Ready / 未报告 | `fd03a30513ff` | `codex/gap-current-status` / #212 | 是 |
| [#216](https://github.com/saksim/vibe_job_radar/pull/216) | perf: 合并列表终态的重复同步落盘（G06） | Ready / 未报告 | `d3e03daed6c0` | `codex/role-sample-coverage` / #214 | 是 |
| [#218](https://github.com/saksim/vibe_job_radar/pull/218) | feat: 增加独立质量标注与评估（G03） | Ready / 未报告 | `9d8c113615ba` | `codex/gather-terminal-checkpoint` / #216 | 是 |
| [#220](https://github.com/saksim/vibe_job_radar/pull/220) | test: 保留整套预算附近的线程快照（G06） | Ready / 未报告 | `67a765cac1ed` | `codex/quality-review-packet` / #218 | 是 |

提交包含不能证明人工验收或发行完成。合入时重新读取main/基底/保护规则，按交接单处理审核、最终源码CI与旧Issue状态。
