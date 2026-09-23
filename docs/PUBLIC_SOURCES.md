# 固定公开招聘来源

关联#95/#30 H2-H4/#3/#9。原Anthropic目录已在main，本分支增加Cloudflare，是否合并以PR为准。两者都只是本机岗位研究入口，默认不授予远端分发权限；不提交申请、不读取账号或个人资料。

## 使用与来源

首页选择公司、输入关键词与地区、勾选本机获取后提交。只请求所选公司的固定公开目录，筛选在本机进行；选择来源或打开网页不会联网。固定单岗位案例按钮继续指向原Anthropic案例。Cloudflare按标题全部匹配优先、再按来源ID排序，正文宽匹配仍保留且明确计数；不会把公司介绍里的架构师关键词排在真正同名岗位前。原Anthropic排序与游标不变。

| 来源 | 固定Job Board GET | 职位身份 |
|---|---|---|
| Anthropic | `boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true` | 已有Greenhouse两个域名，`/anthropic/jobs/{id}`，无查询字段 |
| Cloudflare | `boards-api.greenhouse.io/v1/boards/cloudflare/jobs?content=true` | `boards.greenhouse.io/cloudflare/jobs/{id}?gh_jid={id}`，公司必须为Cloudflare，两个ID一致 |

均为HTTPS。未知公司、任意URL、多来源同时获取、额外认证字段及未核对的职位URL查询不接受。Cloudflare接口中的正文、ID、总数和公司均需完整校验；URL来自原回答，不另发详情请求或推断仍在招聘。远程属性未明确提供时仍为未知。

依据为[Greenhouse官方公开Job Board API](https://docs.greenhouse.io/job-board.html)，其GET无需认证，`content=true`包含正文；受认证的申请POST不在本项目访问范围。[Cloudflare Greenhouse入口](https://job-boards.greenhouse.io/cloudflare)正常跳转其[官方职业页](https://www.cloudflare.com/careers/jobs/)。公开可读与生产转载/分发许可分别处理。

2026-09-23一次真实匿名GET得到HTTP200、382条记录且总数一致，新解析器可读取382条，最短纯文本6421字符。对该次全部正文做字面检索，Cursor、Claude、Copilot、vibe coding均为0匹配；不把数量当作Vibe Coding要求、岗位活跃证明或覆盖率。下一次真实原任务/报告链路查询Architect得到235条宽匹配，旧ID排序的前20条均被原岗位分类过滤；因此新增标题匹配优先。对同一真实缓存重算，无新增请求且保留原采集时间，前20条中的8条纳入原报告完整JD组，已接受Vibe要求仍为0；首次空报告也保留，未放松分类或补造要求。源码与CI不捆绑原始全文，日后来源可改变。中国三站的正常登录/完整JD及跨日期认证继续#49/#50/#54/#55/#56/#63跟踪。

## 状态、额度与报告

两个目录分别缓存、保留硬失败和比较自身的相邻完整目录。新公司首次出现只建基线，不把全部岗位算作新增；同一数字ID属于不同来源。游标绑定来源、查询和完整快照，跨来源或过期游标在请求前拒绝。“下一页”和已保存查询明确显示原来源，修改上方表单不会偷偷改变原结果。

仍共用原`public_examples/rates.sqlite`与同一Greenhouse额度：最小30秒，12次/小时、24次/滚动24小时；429共享持久冷却，失败不退款。换公司、关键词、入口或重启不能重置额度。10分钟缓存、7天内带失败标识的临时故障回退保持；硬拒绝或坏响应不被缓存掩盖。

结果进入原Store、本批报告、CSV、`public_source.json`和`catalog_changes.json`，清单保留文件哈希。每日计划可明确选择任一固定来源，保存不立即联网；24小时后的执行仍遵循原所有权、同意、配额、暂停与重启规则。各报告只含该次所选来源，不混成另一公司的岗位。

## 验证与回退

`test_public_sources.py`使用标注的人工目录验证身份/查询边界、来源隔离、游标、共享额度/429、撤销、旧计划绑定、报告和每日执行；实际Edge/Chromium脚本`run_public_sources_browser.py`验证选择、切换后分页、原报告、独立计划和390px页面。人工夹具里的Cursor要求不代表真实Cloudflare正文。

`check_live_local_public.py --live --source cloudflare`只对该固定目录执行一次真实GET；省略source仍为Anthropic。CI在每个真实请求间留31秒，产物只含计数和元数据。具体提交是否通过须回读CI，不能由脚本存在推断成功。

升级前停止旧服务并备份工作区；回退不要删配额库、缓存或报告。原Anthropic来源契约、缓存名称、默认选择和计划绑定不变。旧版本不认识Cloudflare，留下的新来源缓存和历史报告保留；含新来源的计划不能继续执行，应先停止并核对，而不是替换成Anthropic。仍不提供生产服务、全天运行保证或跨设备全局配额。
