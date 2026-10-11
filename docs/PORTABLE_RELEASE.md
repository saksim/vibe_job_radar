# 从既有 Windows 候选准备发行草稿

关联 #318 / #210。发行入口复用已经通过 main CI 的候选 ZIP，保留原构建记录和原字节，不重新构建、运行程序或重复测试。得到的是**未签名的预发行草稿**，不能当作签名正式发行、实站认证或干净机器验收。

## 维护者操作

在 Actions 选择工作流 prepare-portable-release，仅从 main 手动运行，输入 windows-portable-candidate 的成功 **push/main run ID**。该提交的8个现有验证工作流必须已完成并成功；PR、fork、缺项、仍在运行或失败的检查不能用于发行准备。选择的候选产物尚须在保留期内。运行ID必须使用不含前导零、正号或空格的正十进制格式；同一ID的Actions任务串行运行且不取消正在上传的任务。本机CLI须由维护者避免并发启动相同候选。

脚本仅下载选定运行的 windows-x64-portable-candidate 和 windows-portable-evidence。它核对 ZIP 名称/大小/SHA-256、包内版本/源码修订/执行证据与原源码材料一致，然后创建 GitHub Release 草稿并上传候选、校验表、来源记录和升级说明。不会访问招聘网站、修改用户工作区或安装组件。

标签含软件版本、源码提交、原运行ID和尝试编号，不把相同0.2.0版本的不同候选混为一个包。它保存签名、实站和干净机验收未完成的事实。旧tag或同名草稿已存在就停止，不覆盖资产；发行清单分页核对，创建草稿前再次确认全部8个工作流及尝试编号未变化；上传中途失败保留草稿和准备记录，先查看现状再处理，不能盲目重试或发布残缺草稿。

在已有 gh 登录的本机也可只准备文件：

    python scripts/prepare_portable_release.py --repository saksim/vibe_job_radar --run-id <成功main运行ID> --out .verify/release-draft-<运行ID>

明确增加 --create-draft 才会创建草稿。所有输出目录须是尚不存在的新目录；临时目录亦应配置到项目内部。草稿保持未发布，签名证书、正式发行条件及 #319 的干净机/新手验证仍独立跟踪。

## 用户升级和回退

每份草稿携带 UPGRADE_AND_LIMITATIONS.md、SHA256SUMS.txt 和 provenance.json。先校验 ZIP；停止任务和计划，关闭所有同工作区程序、停用旧登录启动登记，备份完整工作区。新程序解压到新目录，继续选原工作区，保留旧程序和备份。回退先关新版，再使用已验证旧程序及对应备份；使用完整网址 PAC 时先在新版切回兼容偏好。

[便携包使用](WINDOWS_PORTABLE.md)、[工作区兼容](WORKSPACE_COMPATIBILITY.md)和[网络回退](WORKSPACE_PROXY_SETTINGS.md)继续适用。源码启动、已有采集和人工导入、分析、原报告、会话选择与频次限制均保留；新增发行入口不自动升级或淘汰这些功能。
