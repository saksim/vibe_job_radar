# 猎聘已返回正文解析修复：裸换行 JSON-LD 与无 h1 页面

关联 #49 / #46；实施基线为 PR61 已合并的 main `ec4587992113848be879da2fbc4cebdac62d10e2`。

## 这次修复的实际代码缺口

主干通用解析对 JSON-LD 直接使用严格 JSON 解码。若字符串中的换行未转义，整个结构化块会被跳过；之后 DOM 路径和原猎聘语义路径都依赖 `h1`，因此即使独立职位介绍已完整返回，仍会报 `structure_changed`。

已用独立编写的合成正文复现：无 `h1`，`JobPosting.description` 内含裸换行，正文位于 `dd[data-selector="job-intro-content"]`。旧 `DOMAdapter.detail` 失败，新 `LiepinAdapter.detail` 保留完整正文并接入原保存/报告流程。该修复不会替未到达的网络数据创造正文。

## 来源与证据等级

2026-09-18 读取公开仓库 `rockbenben/ai-job-search-cn` 的以下资料，作为页面表示形式的外部证据，不执行其中任何采集指令或复制其程序：

- [接口与解析记录](https://github.com/rockbenben/ai-job-search-cn/blob/main/.agents/skills/liepin-search/url-reference.md)，Git blob `6cb406899a53be8bf10084d832e3b0c8e2f6ae3b`。作者记载的观察日期为 2026-07-23，说明无 `h1`、裸换行 JSON-LD 和上述 `dd` 节点。
- [企业职位 HTML 样本](https://github.com/rockbenben/ai-job-search-cn/blob/main/.agents/skills/liepin-search/cli/tests/fixtures/detail-job.html)，Git blob `1b9306e2f817b6756c00eaf0e3adddca1560a3b0`。本轮检查了 head、标题/资源及职位介绍附近的片段，确认 `section.job-intro-container > dl.paragraph > dt + dd` 形态。**没有把该文件完整运行并声称原样通过。**

这是一份外部维护的历史页面记录，不是 Radar 在用户本机、本次账号会话下抓取的原始页面；也不证明网站当前所有模板相同。回归用例只保留必要的标记结构，正文、公司、岗位 ID 和日期均独立合成，不复制外部完整 JD、用户信息、Cookie 或脚本。没有据此认证真实平台、扩大 API/CDN/登录允许列表或关闭 #49/G1。

## 实现

1. 仅在猎聘适配器内，将 JSON 字符串内部的裸 CR/LF/TAB 转为等价 JSON 转义；不执行 JavaScript，不修补截断、引号、花括号或其他控制字符。仍拒绝重复键及非 JSON 的 NaN/Infinity。
2. 按平台、主机、URL 家族和岗位编号选定 `JobPosting`，允许同一实体的无敏感查询变体。多岗位没有唯一身份匹配时失败，推荐内容不能靠顺序占据正文。canonical 与实际详情检查保留。
3. 有专用 `dd` 时要求唯一、未被明确隐藏，并以选定结构化元数据取标题/公司，不依赖 `h1`。可见正文须与该岗位的结构化描述一致；可见完整正文可以扩展结构化前缀，但不能拼接另一岗位描述。独立结构化描述的裸换行也可修复。
4. 登录提示、未展开、歧义、正文不一致和过大内容均有界失败。不回退整个 body。正文与结构化描述不一致时沿用 `jd_incomplete` 逐条失败，保留其他已取得岗位和本批报告。
5. 原 service → Store → 分析 → 报告不变；沿用原采集审计的 `parser`、岗位身份及正文指纹。新增 parser 标记 `liepin:job_intro_jsonld:v1` / `liepin:jsonld_string_whitespace:v1`。

适配器的会话/网络版本保持 v3，不因为本地解析修复让 PR61 中保存的会话绑定失效。旧有效 JSON-LD、已支持 DOM/语义路径继续回归。无数据库迁移，无生产依赖变更，无新增网络请求或自动登录行为。

## 验证及验收边界

`tests/test_liepin_recorded_layout.py` 包括旧路径复现、字符串保真、结构/身份/内容冲突、推荐隔离、隐藏/摘要/登录、跟踪参数、无 AI 要求，以及原服务中“失败项 + 成功项 → 完整正文落库 → 同批报告”的纵向用例。

原 `run_native_browser_acceptance.py` 增加独立人工 TLS 来源：静态列表 → 无 h1 / 裸换行 JSON-LD / 专用 dd → 实际 LiepinAdapter → Store → 原报告。仅人工测试 origin 增加精确两条文档路径，生产站点契约完全不变；原登录、持久 Cookie、弹窗、限流和证书断言全部保留。

单元测试和受控浏览器通过只证明这些代码路径有效。**真实猎聘的登录、搜索请求、必要依赖及首条完整目标 JD 仍需要连续实站证据；正常密码表单自动登录也尚未由本修复实现。** 当前真实主页能够显示登录入口，不等于完整登录协议已验证，不能以猜测放开全部请求。

## 回滚

回滚本 PR 的猎聘解析增量和对应验收用例即可恢复此前行为；不删除本地 Cookie、岗位、报告、个人证据或配额。旧 bridge 默认与已合并 native 路径保持不变。PR 使用 Refs，不自动关闭 #49 / #50 / #46。
