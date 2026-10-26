# Fork 分支与未合并提交审计（2026-09-16）

总控 saksim/vibe_job_radar#3。Fork 是开发副本，不替换正式仓库。

| 分支/PR | 核验提交 | 相对上游 main | 处理 |
|---|---|---|---|
| 上游 main、Fork main | a4f8e009f9cf03919d0a86b462e7047778e24de6 | 相同 | 两边主干均不改动 |
| feat/loopback-socks5 / #26 | 905a082602273d0c899af6a3aab82df06d634225 | 领先1，落后0 | 已在Fork按相同SHA保留；未混入#31 |
| build/source-bound-qualification-0.2.1 / #29 | 0dad5f6a735d5897181affcecac8295eb95f3f87 | 领先1，落后0 | 已在Fork按相同SHA保留；未混入#31 |
| diagnostics/proxy-environment-gap | 2e8cc1b31777acecb4357d54572b671165691417 | 领先0，落后1 | 已是main祖先，没有独有待合并提交；不重复应用 |
| feat/hybrid-core-phase1 / #31（本次起点） | 52f2f3211a7366310c17f2444b6af66459c770a2 | 领先2 | 在既有PR上继续，保留其两个提交 |

只有main的Fork并没有删除上游分支。领先的提交不能当作main已交付功能，也不能因未合并就漏掉。本审计通过GitHub分支列表和compare结果核对，不按文件时间或版本号推断。

## 组合审查，不直接拼接

PR26与31都修改network.py、guided/service.py、collection_recovery.py、network_environment.py。尤其PR26的显式HTTP/SOCKS选择与PR31的NetworkPolicy快照/NO_PROXY可能发生语义冲突；应把SOCKS接到统一策略里，不能覆盖其中一个选择器就算合并完成。

PR29的源码绑定构建应独立保留；它将版本升级到0.2.1，并已将其tests.yml中的固定0.2.0断言改成安装版本与源码版本相等。整合时需同时保留这些一致性修改、PR31的新增验收路径和现有源码归档，不将维护版本号提升当作采集修复。

两个原PR保持开放、可单独复审；在组合代码实际回归前，不自动合并、不删除原分支、不把各PR的历史测试数字相加。

## 已核实的远端证据：PR31 @ 52f2f321

- offline-tests run 35078576875：7个OS/Python组合均真实执行并成功。
- browser-acceptance run 35078576890：success。
- browser-installation-health run 35078576884：success。
- live-public-example run 35078576891：真实公开GET工作流success；不是招聘三站认证。

源代码来自该run的source-checkout产物10439148512，外层ZIP SHA256：
`4d1089d3219afdbf5d6706a98453f50935cbb6d6a8ad2b10fd34de44930da528`。
解包后按原文件模式重建Git树，精确匹配`18b2c809c127e8423ad38f3387c7f19aeb37a039`。本地再执行原完整478项，全部通过。

这些证据属于本次改动之前的精确提交。新增HTTP转交代码必须另外验证；不能拿旧绿色状态为新提交背书。当前runner已经能够执行，不等于历史无步骤失败原因已查明，也不等于PR26/29重新通过。
