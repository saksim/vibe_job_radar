# 固定公开招聘来源

关联#95/#118/#30 H2-H4/#3/#9。原Anthropic目录已在main，Cloudflare与本分支新增Cursor是否合并以PR为准。三者都只是本机岗位研究入口，默认不授予远端分发权限；不提交申请、不读取账号或个人资料。

#101另增[目录版本校验](PUBLIC_CONDITIONAL_REFRESH.md)：有效ETag的304可确认原目录未变化，正文采集时间保持，确认时间单列；每次仍消耗原共享额度。

## 使用与来源

首页选择公司、输入关键词与地区、勾选本机获取后提交。只请求所选公司的固定公开目录，筛选在本机进行；选择来源或打开网页不会联网。固定单岗位案例按钮继续指向原Anthropic案例。Cloudflare按标题全部匹配优先、再按来源ID排序，正文宽匹配仍保留且明确计数；不会把公司介绍里的架构师关键词排在真正同名岗位前。原Anthropic排序与游标不变。

| 来源 | 固定Job Board GET | 职位身份 |
|---|---|---|
| Anthropic | `boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true` | 已有Greenhouse两个域名，`/anthropic/jobs/{id}`，无查询字段 |
| Cloudflare | `boards-api.greenhouse.io/v1/boards/cloudflare/jobs?content=true` | `boards.greenhouse.io/cloudflare/jobs/{id}?gh_jid={id}`，公司必须为Cloudflare，两个ID一致 |
| Cursor | `api.ashbyhq.com/posting-api/job-board/cursor` | `jobs.ashbyhq.com/cursor/{uuid}`，API版本1、UUID与URL严格一致，无查询或申请路径，仅`isListed=true` |

均为HTTPS。未知公司、任意URL、多来源同时获取、额外认证字段及未核对的职位URL查询不接受。Cloudflare接口中的正文、ID、总数和公司均需完整校验；URL来自原回答，不另发详情请求或推断仍在招聘。远程属性未明确提供时仍为未知。

依据为[Greenhouse官方公开Job Board API](https://docs.greenhouse.io/job-board.html)，其GET无需认证，`content=true`包含正文；受认证的申请POST不在本项目访问范围。[Cloudflare Greenhouse入口](https://job-boards.greenhouse.io/cloudflare)正常跳转其[官方职业页](https://www.cloudflare.com/careers/jobs/)。公开可读与生产转载/分发许可分别处理。

2026-09-23一次真实匿名GET得到HTTP200、382条记录且总数一致，新解析器可读取382条，最短纯文本6421字符。对该次全部正文做字面检索，Cursor、Claude、Copilot、vibe coding均为0匹配；不把数量当作Vibe Coding要求、岗位活跃证明或覆盖率。下一次真实原任务/报告链路查询Architect得到235条宽匹配，旧ID排序的前20条均被原岗位分类过滤；因此新增标题匹配优先。对同一真实缓存重算，无新增请求且保留原采集时间，前20条中的8条纳入原报告完整JD组，已接受Vibe要求仍为0；首次空报告也保留，未放松分类或补造要求。源码与CI不捆绑原始全文，日后来源可改变。中国三站的正常登录/完整JD及跨日期认证继续#49/#50/#54/#55/#56/#63跟踪。

## 状态、额度与报告

三个目录分别缓存、保留硬失败和比较自身的相邻完整目录。新公司首次出现只建基线，不把全部岗位算作新增；岗位ID始终绑定来源。游标绑定来源、查询和完整快照，跨来源或过期游标在请求前拒绝。“下一页”和已保存查询明确显示原来源，修改上方表单不会偷偷改变原结果。

仍共用原`public_examples/rates.sqlite`与同一额度（数据库历史scope名保留）：最小30秒，12次/小时、24次/滚动24小时；429共享持久冷却，失败不退款。换公司、API域名、关键词、入口或重启不能重置额度。两个API域名各自的临时传输阻断只在共同冷却到期后按对应域名恢复，不能误清另一域名，也不能令旧域名永久卡住。10分钟缓存、7天内带失败标识的临时故障回退保持；硬拒绝或坏响应不被缓存掩盖。

结果进入原Store、本批报告、CSV、`public_source.json`和`catalog_changes.json`，清单保留文件哈希。每日计划可明确选择任一固定来源，保存不立即联网；24小时后的执行仍遵循原所有权、同意、配额、暂停与重启规则。各报告只含该次所选来源，不混成另一公司的岗位。

## Cursor公开目录

[Cursor官方招聘页](https://cursor.com/careers)与其Ashby公开职位页核对来源；[Ashby官方公开API](https://developers.ashbyhq.com/docs/public-job-posting-api)说明`descriptionPlain`、`isListed`、`isRemote`和`secondaryLocations`。本分支只接一个固定board，不开放自填token、URL或多个来源同时请求。整个API版本/公开条目身份/正文校验通过后才替换缓存；缺失公开标志不能推断为公开，`isListed=false`的ID、正文和申请字段不进入缓存/变化/报告。

保留官方完整纯文本及其偏移，不抓申请页面。主地区与公开二级地区按来源顺序去重后合并，以免只在第二地区开放的岗位被本地筛选漏掉；remote只采用来源布尔值，缺失则未知。`publishedAt`是来源的最后发布时间；当前统一契约仅保存实际取数时间，不把它混成职位发布时间，旧JobRecord/缓存格式不改。Ashby没有独立`meta.total`计数字段，按已核对的全目录接口契约读取返回列表，不宣称独立验证了市场完整覆盖。

2026-09-24 05:50 UTC的一次本机实际匿名GET返回125条公开列出岗位、48条remote=true，纯文本434—7368字符；这只是来源观察数量，不是目标岗位纳入数量或已接受Vibe要求数量。系统解析首次返回不可接受地址，独立评估工作区临时明确使用原加密解析后成功并恢复原模式；没有改系统DNS或降低公网/TLS验证。原文不进入Git、CI或发行包。

06:06 UTC通过原PublicTasks取数和生成报告：Engineer宽查询返回50条，但原目标岗位分类纳入0条，空报告完整保留。随后Architect查询复用同一10分钟缓存，无新GET、保留原正文采集时间；44条宽匹配的前20条中有6条进入完整JD组，产生108条待复核要求，已接受Vibe要求与Vibe证据组均为0，分析错误0。159个旧报告文件的哈希未变。没有为了增加数量放松岗位分类、补造工具要求或改旧报告。当前提交验证结果在#118及PR记录，受控夹具中的正向要求不能替代这些真实计数。

`test_public_ashby.py`以人工文本覆盖版本/公开标志/UUID/URL/正文错误、未列出过滤、二级地区/remote、原缓存不变、绑定游标、ETag、跨域共享429、原报告和每日执行。`run_cursor_public_browser.py`用真实浏览器验证选择、原报告、编辑表单后的分页归属、计划停用和390px。`check_live_local_public.py --live --source cursor`执行一次默认网络路线下的真实GET，不自动打开加密DNS或扩大来源。

## 回归与回退

`test_public_sources.py`使用标注的人工目录验证身份/查询边界、来源隔离、游标、共享额度/429、撤销、旧计划绑定、报告和每日执行；实际Edge/Chromium脚本`run_public_sources_browser.py`验证选择、切换后分页、原报告、独立计划和390px页面。人工夹具里的Cursor要求不代表真实Cloudflare正文。

`check_live_local_public.py --live --source cloudflare`只对该固定目录执行一次真实GET；省略source仍为Anthropic。CI在每个真实请求间留31秒，产物只含计数和元数据。具体提交是否通过须回读CI，不能由脚本存在推断成功。

升级前停止旧服务并备份工作区；回退不要删配额库、缓存或报告。原Anthropic/Cloudflare来源契约、缓存名称、默认选择和既有计划绑定不变。旧版本不认识新增来源，留下的新来源缓存和历史报告保留；含新来源的计划不能继续执行，应先停止并核对，而不是替换成另一家公司。仍不提供生产服务、全天运行保证或跨设备全局配额。
