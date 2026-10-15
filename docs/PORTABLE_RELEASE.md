# 从合格 Windows 候选准备和发布版本

关联 #318 / #356。发行入口复用已经通过准确 main CI 的候选 ZIP，保留原构建记录和原字节，不重新构建、运行程序或重复测试。默认仍是未签名预发行草稿；可明确选择稳定渠道并在核对完整资产后公开 Tag/Release。稳定渠道与代码签名、真实账号及干净机器现场验收分别记录。

## 晋升与变更说明

功能先进入 dev，再由 dev → release → main 晋升；见[分支晋升](BRANCH_PROMOTION.md)。晋升 PR 与发行说明必须写明“解决了什么、提高了什么、验证结果、剩余问题”，关联实际 Issue/PR。

Actions 输入 change_notes；本机用 --change-notes path/to/changes.md。脚本在下载前检查 UTF-8 Markdown 四段，正文进入 Release、CHANGES.md 与校验清单，provenance.json 保存其摘要。仅本机准备旧包文件时可省略；创建草稿或公开发行不可省略。

## 维护者操作

在 Actions 选择 prepare-portable-release，仅从 main 手动运行，输入 windows-portable-candidate 的成功 push/main run ID。该提交的8个既有验证工作流必须已完成且成功；PR、fork、缺项、运行中或失败不能作为发行依据。运行ID使用不含前导零、正号或空格的正十进制格式，候选工件须未到期。同一ID的任务串行运行、不取消上传；本机CLI由维护者避免并发。

- channel=prerelease、publish=false（默认）：创建含版本、提交、运行ID、尝试编号的预发行草稿，保留原流程。
- channel=stable、publish=false：创建标准 vMAJOR.MINOR.PATCH 稳定版草稿，供核对。
- channel=stable、publish=true：先创建草稿并上传，再逐项核对8资产名称/大小/SHA-256、草稿目标提交及渠道，重新确认准确 main 的8个工作流与尝试编号均未变化，最后公开稳定 Release/Tag 并设为 Latest。

脚本只下载选定运行的 windows-x64-portable-candidate 与 windows-portable-evidence，核对 ZIP 名称/大小/摘要、包内版本/源码修订/执行证据；原生组件、完整网址 PAC 和 DNS WPAD 使用与构建器相同的详细验收。单个绿色标记不能代替执行/清理/回退记录。完整资产为 ZIP、manifest.json、provenance.json、source-verification.json、portable-verification.json、CHANGES.md、UPGRADE_AND_LIMITATIONS.md、SHA256SUMS.txt。

已有同名 Tag 或草稿立即停止，不覆盖资产；v0.4.0-candidate 等不同标签不阻塞 v0.4.0。发行清单分页核对。上传失败保留草稿和准备记录；公开请求网络结果不明时先查看远端，不盲目重复。发布成功还须匿名核对 Tag→准确提交、Release 标记和全部资产。随包 provenance 的 draft 字段表示准备阶段；是否公开由实时 Release 状态决定，候选包内原构建标记保留可追溯性。

本机仅准备文件：

    python scripts/prepare_portable_release.py --repository saksim/vibe_job_radar --run-id <成功main运行ID> --out .verify/release-<运行ID>

明确增加 --create-draft 才创建草稿；增加 --channel stable --publish 才自动核验并公开稳定发行。输出须是不存在的新目录，临时目录亦配置到项目内部。既有0.3.0公开预发行和旧草稿不被覆盖。

## 用户升级和回退

按[版本升级入口](UPDATES.md)或[便携包使用](WINDOWS_PORTABLE.md)执行。先核对 ZIP；停止任务和计划、关闭所有同工作区程序、停用旧登录启动登记，备份完整工作区，再把新程序准备到新目录并选择原工作区。保留旧程序和备份。

[工作区兼容](WORKSPACE_COMPATIBILITY.md)、[网络回退](WORKSPACE_PROXY_SETTINGS.md)及[团队服务迁移](TEAM_SERVICE.md)继续适用。原源码/便携启动、导入、采集、分析、报告、会话选择与访问限制保留。签名证书、#319干净机/新手和生产部署仍按各自证据验收。
