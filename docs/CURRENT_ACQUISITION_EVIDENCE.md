# 已有取数证据与待验收项

整理日期：2026-10-08。以下真实采样和搜索观察保留原日期与提交归属；本次文档整合没有发出招聘站点请求。当前代码、首次CI、真正主干及便携程序包的资格以对应PR验收记录为准，历史开发实例不代表当前用户工作台。

## 已保存的真实观察

| 路径 | 已保存结果 | 验收边界 |
|---|---|---|
| 猎聘公开分类与详情 | 9月24日原89个不同完整职位URL；原报告中49个匹配目标、94条要求（24规则接受、70待复核） | 规则接受不等于人工身份/全文审核或阈值批准；原报告未重算 |
| 第二日期：9月25日 | 顺序采集3项，3份完整正文，均为重复URL；独立报告保留 | 新增不同职位为0，原89条报告摘要保持 |
| 第三日期：9月26日 | 顺序采集3项，2份完整正文、1项structure_changed；成功项中1重复、1新增 | 实采2/3与失败项保留，不能把离线解析修复改写为3/3 |
| 第四日期：9月27日 | 同一1139字分节正文独立复验成功，新增一个不同URL；新报告独立保存 | 首次DNS失败与其后独立预登记成功均保留，没有改写第三日失败或原报告 |
| 截至9月27日累计 | America/Denver的9月24、25、26、27日，共91个不同完整职位URL | 人工确认仍为0；阈值确认、正常账号/会话及整体G2验收仍未由此完成 |
| 猎聘正常输入后选择首个详情 | 历史提交 d6239d9 在可见输入框正常填入“架构师”，提交一次并得到42张卡；按预定顺序选中的办公室文员详情已暂停招聘 | 卡片含非目标职位，所选详情未获得完整正文或报告，密码POST为0；这是部分搜索证据，不是账号或完整工作台流程验收。见[原始实测](https://github.com/saksim/vibe_job_radar/issues/63#issuecomment-5837743943)及[历史诊断](NATIVE_CLOSED_DETAIL.md) |
| 猎聘关键词单页搜索 | 9月25日开发提交1987986经原工作台HTTP接口在Edge取得42张卡，停在选择阶段 | 该提交首次CI为19/21；卡片不是完整JD，未证明账号登录、后续正文及报告闭环，也不代表当前环境可复现 |
| BOSS与51job | 历史所选BOSS详情跳向登录/验证；51job的robots响应为HTML后程序停止 | 原观察不能说明当前站点统一行为，专用链路仍分别由55、56跟踪 |

采样原始口径与成本见[第二日期记录](https://github.com/saksim/vibe_job_radar/issues/54#issuecomment-5829363544)、[第三日期记录](https://github.com/saksim/vibe_job_radar/issues/54#issuecomment-5843759596)及[第四日期独立复验](https://github.com/saksim/vibe_job_radar/issues/54#issuecomment-5853348805)。历史搜索及其首次CI失败见[原提交记录](https://github.com/saksim/vibe_job_radar/issues/63#issuecomment-5838645866)。这些评论在2026-09-30再次只读核对；没有读取或上传完整招聘正文、HTML、账号或私人报告。

## 分节正文误拒绝

第三日失败项返回HTTP200，独立职位介绍与同岗位结构化全文一致，共1139字。它按技术工作分节，再列技能条件，缺少旧规则认可的通用小标题，见[Issue208](https://github.com/saksim/vibe_job_radar/issues/208)。旧209中的六项人工正反例与保存响应的只读重放是解析证据；后者不是新实站成功，原报告与失败分母保持。 9月27日后续独立现场复验已取得相同1139字正文，并经原URL→Store→报告链路保存为新报告；这是另外的实站证据，原第三日2/3和本次首次DNS失败仍在分母中。

当前修复只在唯一可见介绍与同岗位结构化全文相等时识别有界工作分节和资格条件；不放宽身份、隐藏、截断、推荐隔离、正文一致性或原编号条件检查。旧209的首次Windows便携检查失败保留在原PR；后续独立提交即使通过，也不改写原成绩。

## 仍需独立验收

[正常账号、会话及恢复](https://github.com/saksim/vibe_job_radar/issues/50)、[正常搜索到正文闭环](https://github.com/saksim/vibe_job_radar/issues/63)、[身份/全文人工审核及阈值](https://github.com/saksim/vibe_job_radar/issues/54)、[BOSS](https://github.com/saksim/vibe_job_radar/issues/55)和[51job](https://github.com/saksim/vibe_job_radar/issues/56)各保留自己的结果与未决条件。公开分类、人工夹具、源码CI和实际便携程序资格互不替代实站或账号认证；三站不因此自动获得pilot_verified/live_verified。

当前汇总见[专项总控](https://github.com/saksim/vibe_job_radar/issues/46)、[总线](https://github.com/saksim/vibe_job_radar/issues/57)和[GAP汇总](https://github.com/saksim/vibe_job_radar/issues/210)。本页不记录过期的登录冷却时间，不改变访问许可、配额、浏览器默认值、角色规则或历史报告。
