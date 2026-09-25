# 猎聘城市目录依赖

关联 #63 / #49，沿原 PR196 继续。这里只支持正常页面已观察到的城市目录读取，不表示真实关键词或账号已经通过。

## 当前证据

`0d6be3e` 自身 21/21 CI、四种原生模式和实际 exe 既有流程通过后，于 2026-09-25 11:54:35–11:55:26 UTC 完成一次正常无查询入口观察。默认搜索 POST 已通过本机初始化判断并发出；但等待期间没有观察到可用搜索输入框，未输入/提交关键词。原结果是 `search_ui_unavailable`，最终 `halted=true`，页面仍为 `/zhaopin/`，专属 profile 已清理。

记录中新增 `api-dok.liepin.com/api/com.liepin.bd.p.v4.get-all-dq` 的 OPTIONS。该地址不在原契约，按原规则会得到 `resource_domain_blocked`。本次观察脚本未保存首次后端错误及其时间，不能把这一代码推断写成已记录的唯一超时原因；原失败和最终 DOM 均只留本机。最终 DOM 有搜索字段，也不能证明等待时它可见或可点击。

从页面引用的[当前官方 runtime](https://concat.lietou-static.com/fe-www-pc/v6/js/runtime.af8f4a92.js)解析固定 chunk 映射，读取[官方城市组件](https://concat.lietou-static.com/fe-www-pc/v6/js/vendors-node_modules_liepin_antd-city-pc_dist_es_index_js.79a84a40.js)。组件以 GET 获取地区目录，用于城市选择初始化，参数包含地区分区、显示选项和固定来源标记；它不执行账号或岗位写入。组件为 75,344 字节，SHA256 `24813ca59c8a4423342545097ef905b57633dcb8ee1065ad513490b1581f6a38`。源码仅阅读，没有在研究脚本中执行。

同日该域名 `/robots.txt` 实际返回 404、HTML 错误页。按已有共享解析规则，这是规则文件缺失；错误页不得执行脚本。它与 51job 的 200/HTML 无效规则响应不同。该核对只发送 robots GET，没有人工调用城市 API。

## 实施边界

- 仅增加上述精确主机/路径的 GET 及其 OPTIONS，Origin 固定为主搜索站点，保留已有允许请求头集合；POST、文档导航、其他路径和其他 Origin 仍拒绝。
- CORS 预检按各规则声明的实际方法检查。旧搜索、登录预检仍只接受 POST；城市目录预检只接受 GET。
- 原生浏览器自行发送请求，原 robots、TLS、取消、配额、响应上限及失败判断继续生效。地区响应不能成为岗位列表或空结果依据。
- 官方地区组件另外列出的城市建议和批量查询接口没有纳入此次许可。搜索框自身的关键词建议由[正常表单](LIEPIN_NORMAL_SEARCH_FORM.md)单独列明精确 GET；没有扩大到整个 API 域名的任意读写。

人工 HTTPS 覆盖实际 GET 预检、带凭据模式的 CORS、独立 robots 与额度计量，并验证发布方拒绝地区预检时 GET 和后续搜索均不会发送。当前提交实测成绩以 PR 为准，合成流程不替代真实表单/完整 JD/账号验收。
