# Vibe Job Radar｜招聘要求雷达与可举证描述生成器

> **0.1.0 历史 CLI 手册。** 以下保留当时的命令、示例和验收范围，不是当前安装入口。现版本安装见 [Python 高级安装](docs/PYTHON_DISTRIBUTION.md)，当前操作见[文档目录](docs/README.md)。旧版本包名、计数和“尚未实现”只描述当时状态；具体命令以所用版本的 `--help` 为准。

**版本：0.1.0 · Python 3.10+ · 核心零第三方运行依赖 · 中文 CLI/CSV/HTML 报告。**

把“招聘平台上的岗位文本”变成“逐条、可追溯的要求”，再变成“通用能力底座 + 分岗位补充 + 可度量成果表达 + 缺口清单”。

这不是把几个关键词拼成一段看似高级的简历，也不是声称抓完全国招聘软件的万能爬虫。该版本交付可运行的端到端流程；在线搜索及可选模型接口需要自己的凭据和网络条件，实站权限与页面适配必须单独验收。

## 一、立刻跑起来：不需要密钥或安装依赖

解压后在项目根目录运行：

```powershell
py -3.11 scripts/run_demo.py
```

终端会输出报告路径，打开其中的 `report/dashboard.html`。目录中的 `requirements_zh.csv` 可直接用 Excel 查看；`descriptions.md` 是描述模板，`role_descriptions.md` 是分岗位模板。

**演示数据是 11 条人工编写的合成测试记录，包含跨平台重复、错误岗位、过期岗位、只有摘要、否定句和普通算法岗位反例。全部标记为 synthetic，不是本次抓取的真实招聘结果。示例人物与指标也不是用户本人的履历。**

运行回归测试：

```powershell
py -3.11 scripts/run_tests.py
```

## 二、安装成命令行工具

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\vibe-radar.exe --help
```

Linux/macOS 对应使用 `python3`、`.venv/bin/python` 和 `.venv/bin/vibe-radar`。也可安装交付包 `dist/` 中的 wheel：

```powershell
py -3.11 -m pip install --no-index --no-deps dist/vibe_job_radar-0.1.0-py3-none-any.whl
```

下文命令假定 `vibe-radar` 已在 PATH 中；未激活虚拟环境时使用上述完整 exe 路径。项目不支持 Python 3.6；没有宣称在所有 Python/Windows 组合上实测通过。

## 三、真实使用的主流程

### 1. 确定岗位、平台和检索预算

默认岗位是 `domain_algorithm`（垂直领域算法）、`time_series`（时间序列算法）、`architect`（架构师）。默认检索 BOSS直聘、猎聘、前程无忧，另内置智联、拉勾、脉脉、牛客、实习僧的**域名检索配置**。

```powershell
vibe-radar plan --platforms boss,liepin,51job --roles domain_algorithm,time_series,architect --out runs/search_plan.json
```

默认生成 90 条检索任务：3 平台 × 3 岗位 × 2 个岗位别名 × 5 组 AI 编程关键词。`plan` 不联网、不产生 API 请求。设置其他平台或岗位会改变任务数。

这里“支持平台”只表示域名检索与统一数据模型可处理其授权文本，不表示已打通平台私有接口、登录系统或全部页面。新增平台只需增加配置；特殊页面解析则需新增适配与测试。

### 2. 在线发现线索：有自己的 Brave Search API 凭据时

```powershell
$env:BRAVE_SEARCH_API_KEY="你的搜索API密钥"
vibe-radar discover --db data/jobs.sqlite --platforms boss,liepin,51job --roles time_series,architect --max-requests 20 --pages 1 --out runs/discovery_001.json
```

系统按“岗位、平台交错”的顺序分配预算，不会先耗完一家平台再处理其他平台。记录每个查询、请求页数、存储线索、排除原因、预算未执行项和错误。Brave `offset` 按页从 0 开始，最多配置 10 页，每页不超过 20 条。

搜索标题和摘要只以 `evidence_level=snippet` 入库。**即使摘要出现“熟练使用 Cursor”，也不能当成完整职位正文，更不能推断完整 JD 里没有其他条件。**

`completed_with_page_limit` 是已执行配置的页数，`provider_window_exhausted` 是服务返回的检索窗口结束；二者都不等于全站或全市场穷尽。遇到 401/403/429 停止该服务，不伪装账号或绕过限制。

### 3. 补齐有权处理的完整职位文本

真实工作中优先把有权处理的完整 JD 放到 `data/inbox/`，导入：

```powershell
vibe-radar ingest data/inbox --db data/jobs.sqlite
```

支持 JSONL、JSON、CSV、TXT、Markdown，以及能识别独立职位正文的 HTML。JSON/JSONL 的核心结构：

```json
{
  "title": "时间序列算法工程师",
  "company": "真实招聘公司",
  "platform": "boss",
  "url": "https://www.zhipin.com/job_detail/真实职位ID.html",
  "text": "这里是你有权处理的完整岗位职责与任职要求，保留原始分段。",
  "evidence_level": "full_text",
  "source_mode": "manual",
  "is_synthetic": false,
  "rights_note": "记录来源与允许使用范围"
}
```

不要直接将未填写的 `examples/jobs.template.jsonl` 当成真实样本。CSV 支持中文列名：职位名称、公司、平台、来源链接、城市、职位描述。额外 CSV 列不导入；JSON 未知字段则报错，避免悄悄拼错字段。

TXT/MD 默认把文件名当岗位标题，建议添加 `文件名.txt.meta.json`，补充真实标题、公司、平台、URL 与来源信息。HTML 同样可添加 `.meta.json`；解析优先使用 `JobPosting` JSON-LD，其次只识别独立正文容器，禁止整页 body 兜底。

浏览器辅助选区采集脚本见 `scripts/capture_selected_job.js`：它只把用户选中的正文保存为本地 JSON，不访问 Cookie、不自动登录、不请求后台接口。先阅读源码，仅在有权处理内容时使用；完整性仍由采集人确认。

### 4. 可选：获得允许时尝试公开正文抓取

```powershell
vibe-radar fetch --db data/jobs.sqlite --permit-domain zhipin.com --rights-note "填写本次明确获准的访问范围和依据" --limit 10 --out runs/fetch_001.json
```

省略 `--urls` 时读取数据库中的摘要 URL，也可传每行一个 URL 的 TXT，或含 `url` 列的 CSV。HTTP 访问需要明确域名许可、HTTPS、可用且允许的 robots；不可读取 robots（包括 404）时，本实现保守地停止。robots 允许不等于获得其他形式的授权。

不跟随重定向，不访问内网/回环/云元数据地址，不关闭 TLS 校验，不绕过验证码或登录。返回首页、推荐列表、多 JobPosting、登录墙、挑战页或正文结构不明时记录失败，不把它们伪装成成功职位。

**没有全平台实站成功率保证。** 本次交付没有用户搜索 API 密钥、平台授权或模型 API 密钥；联网请求路径通过模拟响应测试，但不是带真实凭据的线上验收。

### 5. 分析、归纳并生成描述

```powershell
vibe-radar analyze --db data/jobs.sqlite --roles domain_algorithm,time_series,architect --out runs/analysis_001
```

必须使用新的或空的输出目录，避免后一次报告覆盖前一次证据。默认按 90 天采集新鲜度筛选；这不是“确认还在招聘”。已明确过期的记录排除，未知发布日期保持未知。

输出核心文件：

| 文件 | 用途 |
|---|---|
| `dashboard.html` | 本地交互筛选与总览，无外部 CDN |
| `requirements_zh.csv` | 中文列名，逐条原文、岗位、能力、强度、来源、偏移 |
| `requirements.csv` / `.jsonl` | 程序接口、精确原文与完整证据结构 |
| `capability_summary.csv` | 全局与分岗位能力并集、共同项、样本内部频次 |
| `descriptions.md` | 通用底座、逐项措辞、已人工确认的候选人观测 |
| `role_descriptions.md` | 三类岗位分别生成的措辞模板 |
| `requirement_evidence_matrix.csv` | 每条要求对应候选人证据，缺失不自动补全 |
| `job_coverage.csv` | 人工逐条映射的覆盖率，不是胜任度或录用概率 |
| `hard_constraints.csv` / `negative_constraints.csv` | 学历、年限、工作方式与否定/禁止条款 |
| `review_queue.csv` / `reviews.template.json` | 歧义、上下文推断、摘要与模型提案的复核队列 |
| `metrics_catalog.csv` | 13 项可观测指标及计算和比较口径 |
| `input_audit.csv` / `duplicate_groups.csv` | 被排除输入、版本与重复关系 |
| `audit_events.csv` / `analysis_errors.csv` | 可观测执行记录与失败原因 |
| `run_manifest.json` | 参数、状态、数据与产物 SHA256、覆盖声明 |

程序持续输出 CSV/JSON/Markdown/HTML；本次另外附带的 Excel 是展示用快照，**CLI 尚不提供运行时 XLSX 导出或 XLSX 导入**。

## 四、怎样从“模板”变成“我可以使用的个人描述”

### 先复核招聘要求

复制 `reviews.template.json`。只把已经审阅的项目改成 `approve` 或 `reject`，填写 reviewer 和 reason；`pending` 保持待处理。例：

```json
{
  "替换为报告中的真实要求ID": {
    "decision": "approve",
    "reviewer": "人工复核人",
    "reason": "确认该条与同一编号中的AI编程要求直接关联"
  }
}
```

复核通过也不能把搜索摘要升级为正文；`role_related` 普通岗位能力不会因一次 approve 变成 Vibe Coding 证据。词义/切分错误需要修订配置或输入，重新运行并保留新版本。

### 再补候选人证据

从 `examples/candidate.template.json` 开始，参照 `examples/candidate.synthetic.json` 的字段格式，但不能复制其合成数字当作自己的经历。

每个证据项目明确 project、capabilities、scope（synthetic/offline/shadow/production）、review_status、reviewer、reviewed_at、evidence_ref。批准本地文件证据还必须填写匹配的 SHA256；外部引用仅登记，不自动访问或宣称验真。**哈希校验只验证文件一致性，不验证经历或数据内容的真实性。**

用 PowerShell 获取哈希：

```powershell
(Get-FileHash .\evidence\report.json -Algorithm SHA256).Hash.ToLower()
```

`requirement_ids` 要填写报告中经本人逐项确认的 ID。仅写 `capabilities` 不会算作满足具体要求；同类能力不代表满足招聘方指定的工具、交付阶段和具体经验。明确生产/上线要求也不能仅靠离线证据计入映射覆盖。

```powershell
vibe-radar analyze --db data/jobs.sqlite --candidate data/candidate.json --reviews data/reviews.json --out runs/analysis_002
```

系统不会发明提升比例、项目数量、收益、服务规模或生产事故数据。没有个人证据时只生成【待填】模板，不能把这些模板当作既成事实投递。

## 五、量化表达的口径

不推荐“精通所有 AI 工具，效率提升十倍”。推荐以下结构：

> 在【项目/本人负责范围】中，将需求拆解为【规格、接口契约和验收标准】，使用【确实使用过的工具】完成【工作】，通过【审查、测试、回归和发布机制】控制质量。在【同口径任务与观察窗口】下，【指标】从【基线】变为【当前值】，样本量【N】，阶段【离线/影子/生产】，证据【路径或链接】。

交付周期相对下降率 = (基线周期 − 当前周期) / 基线周期 × 100%。基线为 0 不计算相对变化。测试通过率由 80% 到 90% 是变化 **10 个百分点**，不写成“提高 10%”。WAPE = Σ|真实值−预测值| / Σ|真实值| × 100%；分母为 0 记为不可计算。

指标定义是本项目建议的观测契约，不是招聘市场已经要求的统一阈值；更不是本次测得的用户成果。改善只写观察结果，不能未经对照设计便全部归因于 AI。

## 六、可选模型增强

默认规则模式离线完成抽取与描述，不需要任何模型调用。使用可选 OpenAI 结构化提案：

```powershell
$env:OPENAI_API_KEY="你的模型API密钥"
vibe-radar analyze --db data/jobs.sqlite --llm-model "你账号可用且支持该结构化输出接口的模型ID" --consent-send-jd --out runs/analysis_llm_001
```

这会把选中的职位正文发给该 API。明确授权后才能发送；密钥不写入报告，不自动加载 `.env`。模型只提议结构化要求，不访问浏览器、不执行 JD 指令。所有提案验证连续原文与索引、能力枚举和上下文锚点，并保持 `needs_review`；模型不能编造来源或直接给自己证明能力。

V0.1 每个选中正文最多一次模型请求，最长正文 25,000 字符，输出上限 6,000 completion tokens；没有模型总预算调度、自动重试或跨运行缓存。先用较小数据库运行。调用失败保留 `analysis_errors.csv`，退出码 2，不静默伪装成功。

## 七、工程结构与扩展范围

```text
src/vibe_job_radar/
  models.py         # 严格记录模型、快照ID、去重指纹
  config.py         # 平台/岗位/能力配置与校验
  defaults.json     # 8个平台域名、3类岗位、11类能力与工具别名
  discovery.py      # 官方搜索API、分页预算、逐查询审计
  network.py        # 域名许可、公开IP固定、TLS、robots、失败边界
  html_parser.py    # JobPosting与独立DOM正文解析
  ingest.py         # 本地授权材料导入与逐条错误隔离
  store.py          # SQLite快照、版本、来源观测与事件
  extract.py        # 逐条切分、正负向、显式/上下文/普通能力
  llm.py            # 可选结构化提案与来源验证
  metrics.py        # 指标定义、数值校验与公式
  synthesis.py      # 并集/共同项、证据映射与描述模板
  report.py         # 中文CSV与本地HTML
  pipeline.py       # 端到端分析、原子输出与审计清单
  cli.py            # plan/discover/ingest/fetch/analyze
```

架构和验收边界见 `docs/ARCHITECTURE.md`，证据规范见 `docs/EVIDENCE_CONTRACT.md`，本次已执行与未执行验证见 `docs/archive/acceptance/0.1.0/ACCEPTANCE_REPORT.md`。真实网站访问规则可能变化；任何新平台或规则变更，都需补原文样本、反例与回归测试。
