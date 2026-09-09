# 投资项目政策归集系统 v0.4 规划稿

> **文档状态：规划稿（目标态），不代表当前代码已实现。**
> 当前可运行版本为 **v0.3**，以根目录 [README.md](../README.md) 与 [docs/VALIDATION.md](VALIDATION.md) 为准。
> 进度跟踪（2026-09-09）：**浙江动态列表全量翻页（387 条/27 页）与真实入库已接通**、**江苏通知公告（TRS jpage recordset）已接通**（见 [接入记录](EXPANSION.md)）；configure-llm/llm-check 命令、Web 模型配置页、qwen-plus 默认、更多省份来源仍为**规划项，未在当前代码中实现**，请勿按本文验收。真实模型只需配置 `LLM_API_KEY/LLM_BASE_URL/LLM_MODEL`（`.env.example`），评测暂未开展。

在原版 v0.2 上完善的 v0.4 目标：从政府官网发现文件、保存网页及附件、提取元数据，用大模型判断投资政策相关性及四类分类，经过校验后落地 SQLite，并支持复核、追溯、导出和定期更新。

**规划接入：国家来源、浙江动态首页，以及重庆、福建、陕西、湖南省级官网栏目。** 这表示已实现相应列表发现和采集流程，不代表整站历史数据或全部省份覆盖。默认真实模型配置为百炼 `qwen-plus`；仍需用户提供自己的有效 API Key。按本次要求暂不开展模型效果评测，官网连通验证使用规则模式。

真实进展跟踪：浙江接通状态见本目录接入记录（实现后补充）；v0.3 的验证历史见 [v0.3 验证记录](VALIDATION.md)。

## 1. 快速启动

Python 3.10+。在仓库根目录执行：

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS / Linux 改用：source .venv/bin/activate
pip install -r requirements.txt
python -m policy_collector.cli init-db
python -m policy_collector.cli demo
python -m policy_collector.cli web --open
```

打开 `http://127.0.0.1:8000`，可以查看政策、正文、附件原件、分类理由、来源关联、历史版本和运行日志，完成确认、调整分类、剔除等复核操作。界面默认为大模型分类，也可选择规则演示。

`demo` 使用明确标注的合成材料，不代表官网采集成果。建议先在单独的数据目录演示，避免与实际政策混用。PowerShell 设置 `$env:POLICY_DATA_DIR="D:\policy-demo"`；Linux 设置 `export POLICY_DATA_DIR=/path/to/policy-demo`。删除此环境变量后恢复默认的 `data/`。

## 2. 配置真实大模型

默认接入阿里云百炼北京兼容地址和 `qwen-plus`，支持替换为控制台提供的业务空间专属地址；也可选择单位许可的其他兼容服务。预设服务参数不等于已经取得密钥或已连通账户。

**方式一：界面配置。** 启动 Web 后，进入导航栏“模型配置”，填写服务地址、模型名和 API Key，保存后可点击“检查已保存配置的连接”。密钥不回显，仅写入本机数据目录的 `llm.local.json`（Git 忽略），不要将此文件发给别人。连接检查只发送一条最小 JSON 请求，不做分类评测。后续新采集任务自动使用保存配置。

**方式二：命令行配置。** 在仓库根目录执行，密钥使用隐藏输入，不放入命令历史：

```bash
python -m policy_collector.cli configure-llm --provider dashscope
python -m policy_collector.cli doctor
python -m policy_collector.cli llm-check
python -m policy_collector.cli run --source zjfgw_gsgg --limit 5
```

其他服务：`configure-llm --provider custom --base-url <接口地址> --model <模型名称>`。切换到不同地址时必须重新填密钥，程序不会把旧服务密钥自动发送给新服务。

**方式三：环境变量。** 适合不落盘密钥或服务器运行：

```powershell
# Windows PowerShell
$env:DASHSCOPE_API_KEY="你的百炼北京地域密钥"
# 如使用其他地址或模型，可另设 LLM_BASE_URL、LLM_MODEL
python -m policy_collector.cli doctor
python -m policy_collector.cli llm-check
```

Linux/macOS 使用 `export DASHSCOPE_API_KEY=...`。通用 `LLM_API_KEY` 优先于供应商密钥和本地密钥；`LLM_BASE_URL`、`LLM_MODEL` 优先于本地配置。本地配置优先于 YAML。`configure-llm --env-only` 只保存服务参数，不询问或保存密钥。程序不会自动执行上述连接检查，也没有替你开通付费模型账户。

百炼地域与密钥必须匹配；新业务空间域名和兼容旧地址见[百炼官方接口说明](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)。默认关闭思考模式，便于非流式 JSON 分类；其他兼容服务不自动发送这个供应商参数。

分类读取标题、文号、发文机关、正文及已解析附件，按字符分段逐段判断并汇总多标签。默认每段 10000 字符、最多 24 段；超限明确标记 `input_truncated` 并进入待复核，不宣称已读完。页眉、表格及扫描件仍需要核验。

系统检验类别白名单、相关性枚举、布尔值、置信度和引用是否出现在输入原文；引用存在只能证明可定位，不能代替语义正确性评价。模型失败或未配置时记录 `rule_fallback` 和原因，候选进入待复核，不会静默当作大模型成功。分段上限、超时与重试在 `config/config.yaml` 调整。

首次用规则模式采集后，可重新分析尚未人工确认的记录：

```bash
python -m policy_collector.cli run --source ndrc_ghxwj --reclassify --limit 20
```

人工确认、人工调整和剔除记录不会被自动重分类覆盖。模型通过使用 `confirmed_auto` 状态，表示自动校验通过，**不等于人工确认**。

## 3. 按任务划分的模块

| 模块 | 实际职责 | 主要文件 |
|---|---|---|
| 来源与列表 | 栏目白名单、详情 URL 规则、HTML/公开 JSON 列表、TRS 翻页 | `config/sources.yaml`、`collector.py`、`site_adapters.py` |
| 下载与解析 | 限速、重试、大小限制、网页原件、PDF/DOCX 附件、日期与文号 | `collector.py`、`parser.py` |
| 政策研判 | 是否属于投资项目政策；四类多标签；原文证据校验；回退与复核标记 | `classifier.py`、`llm_client.py` |
| 去重与入库 | 文号及内容指纹、转载来源关联、附件变化版本、事务入库 | `dedup.py`、`db.py`、`pipeline.py` |
| 周期运行 | 增量发现、轮转复查已采 URL、失败补采、进程互斥、运行日志 | `scheduler.py`、`locking.py` |
| 使用与验收 | 检索、人工复核留痕、原件下载、JSON/CSV 导出、人工标注评测 | `webapp.py`、`cli.py`、`scripts/evaluate.py` |

```mermaid
flowchart TD
  A[官网栏目与公开列表接口] --> B[候选链接与采集记录]
  B --> C[网页原件和附件解析]
  C --> D{同文号与内容指纹}
  D -->|相同| E[关联转载来源和最近检查]
  D -->|新增或变化| F[大模型分段研判]
  F --> G{输出校验与材料完整性}
  G -->|可用| H[分类与版本入库]
  G -->|失败或不确定| I[显式回退或待复核]
  I --> H
  H --> J[人工复核与变更留痕]
  J --> K[检索和导出]
```

四类分别为 `guide` 引导、`access` 准入、`guarantee` 保障、`incentive` 激励约束。同一政策可包含多类，不能机械按标题归类。具体单个项目批复、新闻、解读、采购公告通常不作为政策正文归集，边界口径由业务人员核定。现有关键词模式只是基线，可能误收或漏收，所有非排除结果均待人工复核。

## 4. 来源、定期采集与补采

```bash
python -m policy_collector.cli sources
python -m policy_collector.cli run --source gov_latest --prefer rule --limit 5
python -m policy_collector.cli run --source cq_normative --prefer rule --limit 5
python -m policy_collector.cli run --source all --limit 20
python -m policy_collector.cli run --source ndrc_ghxwj --retry-only --limit 10
python -m policy_collector.cli schedule --source zjfgw_gsgg,fj_normative,sx_normative,hn_local_rules,cq_normative --interval 60 --limit 20
```

`run` 默认使用大模型，`--prefer rule` 明确选择规则基线。`schedule` 立即运行首轮，之后每轮完成后间隔指定分钟，进程需要持续运行；本次交付没有在后台替你部署常驻任务。可用 `--cycles 2` 验证两轮后退出。缺省只运行启用且非本地演示的来源，Ctrl+C 在当前工作结束后退出。服务器可使用系统定时器调用单次 `run`。

每次既发现新链接，也按最近检查时间轮转复查已有链接。`--limit` 是每来源本轮最多处理的文件数，**不是已覆盖整站**；持续大量新增/失败时旧记录复查可能推迟。`max_pages` 是发现页数上限，历史补录需扩大页数并分批运行。

失败候选留在 `fetch_records`；附件下载失败可补采。没有发现链接视为来源异常，不伪装成成功。运行状态区分 `ok / partial / failed`，CLI 分别返回 0 / 1（运行有异常或模型回退）/ 2（参数配置错误）。发现列表成功不表示所有文件或附件成功，应一起查看运行统计。

省级来源配置如下：

| 来源名 | 地区 / 栏目 | 列表方式与范围 |
|---|---|---|
| `zjfgw_gsgg` | 浙江 / 行政规范性文件 | 读取官网页面内 `queryData`，GET 同域公开 JPaaS 单元接口，解析 `data.html`；已接通动态首页15条 |
| `cq_normative` | 重庆 / 行政规范性文件 | 静态政策汇总页，限定行政规范性文件详情路径 |
| `fj_normative` | 福建 / 行政规范性文件库 | 静态文件库，排除下载按钮与其他栏目 |
| `sx_normative` | 陕西 / 行政规范性文件 | 静态栏目，限定年月和正文详情路径 |
| `hn_local_rules` | 湖南 / 地方性法规规章 | 静态栏目，限定本域地方性法规规章详情 |

浙江每轮重新读官网单元参数，保存栏目原件与接口 JSON，不执行网页脚本，不硬编码公开页面的单元 ID。**目前只验证首页，强制 `max_pages=1`；未完成历史翻页，不把重复首页计为多页。** 已知 URL 仍参与周期轮转复查；若两次采集间新增超过首页容量，可能漏发现，需要补充历史发现策略。新增链接的完整标题优先取 `title` 属性。

浙江新旧正文路径均可识别；附件通过 `download` 属性、`fileName` 查询参数识别，保留官网给出的完整下载链接。遇到403等失败保留链接和补采状态，不绕过访问限制。停用来源不能由 `run/schedule` 执行。


## 5. 数据、去重与人工复核

SQLite 默认 `data/policy.db`，网页及附件按 SHA-256 不可变留存在 `data/downloads/<source>/`。保留原文链接与正文位置；缺失日期或文号留空，不用猜测值填充。发布日和成文日分别存储。

- 同文号、正文与附件指纹一致：不重复插入，保留转载来源。
- 同一原网址正文或附件变化：追加版本并重新待复核，保留历史版本及历史人工决定。
- 同文号但其他网址内容不同：保留独立待复核记录和关联键，不擅自认定为正式修订。
- 无文号：使用发文机关/来源与完整标题，保留年份、试行等括号内容；宁可待复核，不强行跨站合并。
- 网页版式、正文空白变化不生成新版本。纯元数据变化目前只留在抓取原件，不自动更新政策字段；网址恢复历史相同版本不会把旧版覆盖成当前版。版本号代表采集内容快照，不代表法律修订次数。

主要表：`source_configs` 来源；`fetch_records` 发现和下载状态及分类快照；`policies` 政策内容与版本；`attachments` 附件与解析结果；`policy_sources` 转载来源；`review_events` 复核前后快照；`run_logs` 运行结果。旧库启动时补充字段和表，升级前请备份数据库及下载目录。

```bash
python -m policy_collector.cli query --review pending
python -m policy_collector.cli audit --id 1 --action adjust --category guarantee,incentive --note "已核对资金支持和绩效条款"
python -m policy_collector.cli export --format json --out data/policies.json
python -m policy_collector.cli export --format csv --out data/policies.csv
```

默认查询、导出仅含当前版本并排除已剔除项；待复核项仍包含在候选库中。正式使用前可分别按 `--review confirmed`、`--review adjusted` 导出人工通过项。JSON/CSV 均含正文、附件和来源信息，CSV 对公式起始字符进行转义。

PDF 文本和 DOCX（含表格）可以解析；扫描 PDF 标记需 OCR。OFD、旧 DOC、XLS/XLSX、压缩包等当前保留原件、标记尚未解析；不把它们当成完整阅读。默认每文件最大 20MB，PDF 最多 500 页，可根据运行环境审慎调整。附件解析缺失时进入待复核。

## 6. 测试、效果评价与交付边界

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

回归测试覆盖全文及附件输入、JSON 验证、年度文号区别、转载、版本变化、失败刷新、旧库迁移、事务回滚、定时来源筛选、网页复核和来源过滤。模拟模型只能验证接口与流程，不能验证模型理解能力。

业务人员独立标注真实样本，每行一个 JSON，示例结构为：

```json
{"id": 1, "relevant": true, "categories": ["guarantee", "incentive"]}
```

这只是格式示例，**不是对数据库 ID 1 的实际标注**。使用覆盖四类、非相关与边界材料的人工标注集执行：

```bash
python scripts/evaluate.py --db data/policy.db --gold gold.jsonl
```

输出相关性精确率/召回率、类别微平均指标、整篇完全一致率，以及 pending、模型回退数量。该脚本只评价已入库且标注的候选，不能衡量网站发现召回率；应另外抽查官网栏目是否漏采，并查看被排除的 `fetch_records.classification_json`。业务验收还需记录关键字段准确率、附件成功率和每篇人工核验耗时。

**后续事项：** 用户填入实际模型密钥并自行检查连接，模型效果评测按要求暂缓；浙江历史翻页和更广省份覆盖继续扩展；按真实附件补充 OCR/OFD/WPS/旧 Word 解析。现阶段不应汇报为全国覆盖或可无人审核直接用于正式政策适用判断。
