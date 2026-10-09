# 猎聘当前安全组件的固定配置读取

关联 [Issue #63](https://github.com/saksim/vibe_job_radar/issues/63)。本项修复当前访问契约遗漏，旧空白页、瞬时导航错误、真实账号与完整岗位链路仍分别验收。

2026-10-09 09:03 UTC，已资格验证的 main 57c4b540 使用原生 Chrome 157、原网络设置和共享频率账本时，正常搜索入口返回200，随后 dalisi4api.tongdao.cn 的 GET XHR 首次触发 resource_domain_blocked。任务在提交关键词之前停止，0卡片、0正文、0报告、0登录动作。观察代码没有改变命令参数或结果，但可能影响时序；前次 page_not_ready 未重现，不能据此宣称修复其根因。

同日从[官方搜索页](https://www.liepin.com/zhaopin/)确认其引用的[官方安全脚本](https://concat.lietou-static.com/fe-lib-pc/v6/security/1.0.0/security.min.js)。仅阅读原文，没有执行研究副本。当前脚本251791字节，SHA256 263a847d479836a036f4a52fd8ad248d5aa3736e90067856f272348652aac210；9月保存副本为248994字节，SHA256 1c9aa56a327f6138f91ba2104e1933c7be27027e8cc118ec59ef40e81725d919。同一路径内容发生变化，不能仅按版本目录判断静态内容相同。

当前公开脚本指定 https://dalisi4api.tongdao.cn/static/cfg/v2.json，原始读取使用 GET、credentials: omit、cache: no-store。配置供安全组件自身使用，不能归为营销或统计并在本地忽略。

原生契约更新为 liepin_search_login_v4，只允许上述 HTTPS 主机和精确路径的 GET Fetch/XHR。沿用必要读取类别，使独立 robots 检查、配额、TLS、响应上限、取消以及发布方401/403/429判断继续生效；不增加 POST、OPTIONS、文档导航或其他路径。浏览器自行产生和处理请求，配置原文直接交给网站；没有替换配置、修改安全脚本、调整检测条件或模拟验证成功。

配置观察使用独立操作名，不能提供岗位卡片或确认空结果。站点登录、验证码、拒绝或清空页面继续正常停止流程。

新增离线回归在旧规则上复现4个失败及2个错误，覆盖精确边界、原生转发、独立robots/额度、拒绝处理和非岗位隔离。人工HTTPS验收追加配置成功到原完整JD/报告，以及配置403阻止后续搜索两项；只使用合成响应，不执行或复制第三方安全代码。Windows临时CA仍限既有CI，个人主机未安装测试根证书。当前PR的实际验证成绩另行记录，不以合成通过声称实站或真人登录已完成。
