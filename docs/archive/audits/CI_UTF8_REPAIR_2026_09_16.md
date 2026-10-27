# PR #31：Windows 中文任务测试修复

Refs #3 / #28 / #31。基线 f7c07cf956c4bc541b473606598008f059749773。

## 实际失败，不归因于付费

上游 workflow run 35082168741 已执行测试。Windows 3.10/3.11/3.12 失败，
Linux 三组合与 macOS 通过；同提交浏览器、安装健康与公开 GET 工作流通过。
Windows 3.12 job 104752332399 的日志明确显示：

- `test_existing_conflicting_record_never_overwritten`：189 行 `path.read_text()`。
- `test_parent_secrets_diagnostics_not_copied`：205 行 `read_text()`。
- 两项均用默认 cp1252 读取 UTF-8 中文 JSON，抛出 `UnicodeDecodeError`。

本次是测试文件读取编码错误，不是计费阻断，也不是上一轮 SQLite 连接未关闭的问题。
不由此反推更早无步骤 CI 的根因，不要求升级套餐。

## 修复与回归

测试读取/改写任务 JSON 明确使用 UTF-8，保留中文而不改成 ASCII 测试数据。
一并修复真实浏览器脚本中两处默认编码读取。生产任务读写原本已经显式指定 UTF-8，
没有修改生产存储格式、区域设置或任何网络/配额/隐私约束。

新增：项目 Path 文本读取静态回归、任务夹具写入编码回归，以及中文/重音/emoji
在模拟 cp1252 默认环境下的创建、重读、预览、重复提交回归。
本地已用同样的 cp1252 模拟稳定复现旧版两项错误；它不是实际 Windows runner 证明。

不能跳过 Windows、移除断言、吞掉 UnicodeDecodeError，或以 PYTHONUTF8=1 掩盖遗漏。
最新提交是否通过，以对应 SHA 的新 CI 为准，旧绿色检查不作替代。

## 当前交付目标的补充决策

用户已明确：先在本机跑通功能与完整流程，再考虑 Linux 生产服务。
生产地址、域名、付费服务器与云部署不是当前本地版本的验收门槛。
混合架构保留为可替换的数据提供方式；当前本地采集/报告不应依赖自建公网服务。
服务器部署与运营留在后续阶段，不把用户尚未部署的服务当成本地功能故障。

来源：Python 官方文档 https://docs.python.org/3.12/library/io.html#text-encoding 。
