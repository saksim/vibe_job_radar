**历史记录｜2026-10-06恢复**

> 本页原文来自旧分支归档，保留当时的判断、首次失败和测试记录。原文中的“当前”、检查数量及待办均指原记录时点，不代表现行主干已经通过或仍然缺失。

原d6239d9包含真实关键词输入并出现42张卡片，随后首个详情不可用并暂停。这是部分搜索证据，不能说搜索从未成功，也不能据此认证完整正文、报告或登录。当前job_unavailable仍按终态处理。

现行实现与验收边界：[docs/NATIVE_VISIBLE_SEARCH_FORM.md](NATIVE_VISIBLE_SEARCH_FORM.md)；[docs/LIEPIN_PASSWORD_LOGIN.md](LIEPIN_PASSWORD_LOGIN.md)；[tests/test_native_closed_detail.py](../tests/test_native_closed_detail.py)。

[原始归档文件](https://github.com/saksim/vibe_job_radar/blob/198798667119e354080c444993dc934986c9f17d/docs/NATIVE_CLOSED_DETAIL.md)；原文SHA256：9b47efc5d2b74b74a4bc39d554043cca3fec7765b2f7d2ea0e68dbe245198be4。本次恢复只补回历史文档，不重新采集或重算旧结果。

---

# 暂停招聘详情保留原状态

Refs #63 / #49 / #53。`d6239d9`实站正常输入“架构师”并提交一次，当前关键词搜索响应与URL配对通过，返回42张卡片，其中包括非目标职位。探针按第一张卡片选择了办公室文员；详情明确“该职位已暂停招聘”，随后展示其他推荐岗位。最终DOM已保存，离线调用原详情解析器返回`job_unavailable`，而浏览器就绪循环将其吞掉并在原期限结束后报`page_not_ready`。[原始实测](https://github.com/saksim/vibe_job_radar/issues/63#issuecomment-5837743943)。

现在仅将`job_unavailable`作为已确定的终态向上传递，让原采集结果分类说明该岗位已暂停/结束。不会延长等待、重导航、改取推荐岗位、生成假正文或把搜索通过视为完整链路通过。暂缺正文与正常导航替换仍在原15秒期限内观察，登录/挑战、原生拒绝及取消规则不变。

三项人工页面回归在旧实现有两项失败：关闭状态及一次页面替换后的关闭状态都被误报超时；部分正文到完整正文的等待已通过。修复后应保留终态且不再等待或导航，并继续验证原完整正文、导航替换及限制行为。实际新提交的浏览器、完整测试与网站结果单独记录，不把离线复现冒充新实站成功。
