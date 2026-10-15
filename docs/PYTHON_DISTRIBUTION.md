# Python 安装包与独立发行

关联 #372 / #30 H6 / #57。Python包供已有Python3.10+的Windows、Linux和macOS环境使用。它包含产品Python模块及网页资源；不包含Python解释器、Chromium或可选SDK，不是免安装桌面程序。现有Windows便携包、自带浏览器与自动升级照常使用。

## 安装与启动

下载正式Python Release中的 wheel 与 SHA256SUMS.txt，并核对摘要。使用独立虚拟环境，不覆盖现有源码环境或正在运行的程序：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --no-index --no-deps ./vibe_job_radar-VERSION-py3-none-any.whl
.venv/bin/vibe-radar-ui --doctor
.venv/bin/vibe-radar-ui
```

Windows使用 `py -3 -m venv .venv` 和 `.venv\Scripts\python.exe` / `.venv\Scripts\vibe-radar-ui.exe`。默认沿用主目录中的原工作区，可用 `--workspace` 明确选择位置。升级/回退前先停止任务和所有实例，备份整个工作区；保留旧虚拟环境以便回退，不删除工作区。

浏览器采集需要按原页面指引单独安装可选SDK与匹配浏览器；联网安装需用户主动执行。PAC和团队服务也沿用pyproject中的可选依赖；缺少可选组件不表示原导入、分析、报告不能使用。Windows特有DPAPI/PAC/启动/自动替换范围保持，其他系统不能冒用Windows加密凭据。

## 分发与验证

Windows的 `vVERSION` 正式Release继续保留8项资产和Latest身份；Python包使用独立 `python-vVERSION` Tag、正式非预发行且不抢占Latest，两个Release相互链接。旧Windows升级器不需要理解新附件。Python包只在GitHub发行，不上传PyPI。

构建在独立临时目录，仅复制已核对的产品包、pyproject、README、LICENSE；生成wheel、sdist和来源/文件摘要清单。CI在Windows、Linux、macOS全新虚拟环境离线安装同一个wheel，离开源码目录验证五个入口、全部网页资源、人工JD、原报告与独立进程重开。sdist另在源码目录外重建并核对全部产品字节；不会用新包名冒充已验证产物。完整主干CI仍是发行前提，不额外复制全量单测。

每次正式发行的三系统安装、源码复建和准确提交证据随CI_EVIDENCE.json提供；请以该版本所列结果为准。系统浏览器/桌面环境、Linux浏览器系统依赖、macOS签名/公证及干净机器实测另列；此处人工岗位只测试流程，不是招聘站数据或登录认证。
