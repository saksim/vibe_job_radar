# 原生 CI 等待诊断（#111 / #112）

PR72 候选 6a4cfc41 的首次 CI 18/19；Windows headless 的人工 TLS 搜索在首个 create 后 45 秒超时。源端只见两个 robots GET 和一个入口 GET，尚无业务 POST。原始失败、日志及工件已冻结，原因未知。前一步成功的 diagnostic.json 以及导航前的 search-probe 快照不足以解释这次超时。

本补丁从当前已验收 main 9159ce14 独立交付 PR112 的适用诊断，并扩展到搜索：

- 仅在显式 --controlled 且 CI=true、GITHUB_ACTIONS=true 的隔离 CI 包装器启用，不用于用户工作台。
- 内存记录固定方法名及 CDP 命令白名单、进入/离开、相对时间；最多 16 个 backend、每个 32 个等待项、128 条历史。超出计入 dropped。
- 独立观察线程每 5 秒保存一次快照，最多 36 次；快照不会被后续 close 清理覆盖。方法回调不写磁盘，不调用其他线程的浏览器或服务方法。
- 每 35 秒保存一次无局部变量的 Python 线程栈；失败时另保存栈和最多 8 层异常类型，不保存异常文本、方法参数、页面、URL、凭证或 CDP 载荷。
- search-wait-* 保存到 native，retry-wait-* 保存到 native-read-retry，既有 always 工件步骤保留失败现场。

搜索/有限重试的业务测试源码、原 45 秒断言、生产代码、请求预算及所有权均不变。观察器失败不会吞掉原始业务异常。补丁验证的是诊断能力；即使下一候选通过，也不意味着旧超时的准确根因已查明，更不代表真实账户登录或平台采集认证。

## #263：浏览器到本机守卫的连接事实

393761b 的 Windows Edge headless 首轮在既有“发布者 OPTIONS 拒绝”断言处失败：
应为 http_403，实际 local_proxy_connection_failed。前13项通过，失败请求尚无源端
OPTIONS；守卫仍存活、未关闭，29次 accept/dispatch，无记录到的上游 open 异常。
这些事实不能证明 TCP 失败的物理原因；其他浏览器通过也不能覆盖这次失败。

受控搜索包装器现在为每个隔离浏览器启用 Chromium 的有界 NetLog，关闭浏览器后
仅输出固定事件名、数字来源/依赖关系、底层网络/系统错误码、相对时间和
“是否自有代理端点/人工源主机”的布尔分类。URL、地址、请求头、正文、凭据、
原始 constants 和命令行不会进入工件。原日志在仓库 .verify 下的独立私有临时目录，
不属于 artifact 路径，处理后仅清理该目录；失败/缺失/截断明确标记，不代表零错误。
浏览器日志16MiB环形上限、解析20MiB上限、输出最多4096条，始终不声称完整历史。
只在显式隔离 CI 启用，不改变生产代码、代理/认证、请求、重试、超时或原测试断言。
新候选通过不自动关闭历史未知根因。

格式依据：[Chromium NetLog](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/net-log.md)、
[网络事件定义](https://chromium.googlesource.com/chromium/src/+/HEAD/net/log/net_log_event_type_list.h)、
[日志参数](https://chromium.googlesource.com/chromium/src/+/main/services/network/public/cpp/network_switches.cc)。


同一393761b的Windows3.11首轮2758项另有1失败：发布者延迟恢复测试已自动恢复，
两卡均ok，5秒总窗口结束时仍在最终报告analyze等待，两个写入线程在atomic_text/fsync。
这未确定磁盘延迟原因。用受控“第二份报告等待测试的完成等待阶段”屏障复现旧断言，
再将原5秒约束收窄为自动调度发生；后续报告使用同fixture既有完成等待。
仍必须由原后台线程自行到期恢复、保留原选择、首个职位只读取一次并最终completed。
不手动调用恢复、不加产品重试、不改产品期限；首次CI失败继续保留。


清理暂时失败时仅缓存已脱敏的连接事实，不缓存“无需再清理”的结论；
后续close和整套受控搜索结束的finally会再尝试同一自有目录，记录尝试次数。
只有清理成功才直接复用最终结果；无需重读原始文件。持续失败明确保持pending，
不能取得本轮完整交付资格。新回归模拟一次文件占用再释放，验证最终清理、
原证据和业务异常保留。新测试的文本读写显式UTF-8，原编码全库守卫不变。


最终私有日志清理也是CI探针的完成条件。逐个处理所有自有capture，一处清理失败不能跳过其他目录；异常只保留固定observer_error/pending，不序列化私密异常文本。清理后先写脱敏probe结果与private_logs_removed，再在业务本身成功但清理未完成时以固定异常使探针失败；已有业务异常保持原对象传播。原始日志仍不进入上传路径，不增加生产网络请求、重试或等待期限。
