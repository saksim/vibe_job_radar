**历史记录｜2026-10-06恢复**

> 本页原文来自旧分支归档，保留当时的判断、首次失败和测试记录。原文中的“当前”、检查数量及待办均指原记录时点，不代表现行主干已经通过或仍然缺失。

保留原1447d84可选预检被拦截的阶段、官方脚本摘要和局部拒绝理由。历史前端脚本与当时测试数量不能作为当前网站或主干状态。

现行实现与验收边界：[docs/NATIVE_SEARCH_READ_DEPENDENCIES.md](NATIVE_SEARCH_READ_DEPENDENCIES.md)；[docs/LIEPIN_PAGE_DEPENDENCIES.md](LIEPIN_PAGE_DEPENDENCIES.md)；[tests/test_native_acquisition.py](../tests/test_native_acquisition.py)。

[原始归档文件](https://github.com/saksim/vibe_job_radar/blob/198798667119e354080c444993dc934986c9f17d/docs/LIEPIN_HOTWORDS.md)；原文SHA256：91856d61b0aef501861c169c4ae873b6d3096b828d854b7aa943a286fee8fc98。本次恢复只补回历史文档，不重新采集或重算旧结果。

---

# 可选热门词不应中断正常搜索

2026-09-25 的候选 `1447d84` 完成自身 21/21 CI 后，唯一实站观察在无关键词入口首次记录 `native_operation_unreviewed`。首个被拒绝请求是 `api-c.liepin.com/api/com.liepin.searchfront4c.pc-hot-search-word-list` 的 OPTIONS，声明 GET。此时尚未输入关键词、提交登录或获得新 JD/报告；原结果与首错保持，不把这次失败写成登录成功或主页面跳白。

已保存的[官网公共脚本](https://concat.lietou-static.com/fe-www-pc/v6/js/common.0081c3a9.js)（SHA256 `865b29d63791601dbfd17e6f5186bf235d20a1fc7326e8b36007b42ec5aa9f75`）在模块 23400 中将热门词与输入建议分别定义。热门词单独加载，成功时只显示前八个，失败分支为空；输入框和 Enter 提交在其外部。热门词点击是另一个用户事件，不是本任务的关键词或岗位列表依据。静态研究未执行该脚本或人工调用 API。

本次只将该精确主机、路径的 GET/OPTIONS 标为可选且本地中止。浏览器收到正常的本地请求失败，由站点自身失败分支处理；没有伪造成功响应、放行 API、增加网络权限或替换关键词。取消和网络策略撤销仍先处理。其他方法、文档/脚本资源、路径后缀和未知接口保持原拒绝行为。

新回归先在旧代码上实际复现任务终止，再检查本地阻断、不消耗请求配额、不产生岗位观察、完成事件不误报必需网络失败，以及上述拒绝边界。真实浏览器的人工 HTTPS 流程同时发出简单 GET 和需要预检的 GET，验证 GET/OPTIONS 均未到达服务器、原关键词仍只提交一次，并沿用既有完整 JD 与原报告断言。原营销请求并发和其他场景保持。人工结果不是招聘站账号、真实搜索或长期会话验收；新候选实际成绩见原 PR196 / Issue63。
