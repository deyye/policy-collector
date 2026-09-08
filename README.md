# 政策文件归集系统（policy-collector）

> 面向发改/投资条线"**定期采集投资项目政策文件并落地数据库**"的问题解决型任务的初版工程实现。
> 目标：把"官网采集 → 识别分类 → 去重入库 → 可查询、可追溯、可重跑"做成一条能闭环、能演示、能验收的流水线。

## 一、解决的问题

发改委官网政策文件分散在不同站点/栏目，靠人工定期翻查容易漏采、重复收集、无法追溯原文和版本变化。本系统把这一过程自动化：

1. **定期发现**：按来源配置抓取栏目列表页，筛出候选政策详情链接；
2. **识别与分类**：判断一份文件是否属于"投资项目政策"，并按条款实际作用分为 **引导 / 准入 / 保障 / 激励约束** 四类（可多标签）；
3. **入库落地**：提取标题、文号、发文机关、日期、正文，保存附件原件，写入数据库；
4. **去重与版本**：同一政策转载不重复入库；内容更新则追加新版本；
5. **可复核**：不确定的文件标记"待复核"，支持人工确认/调整/剔除，运行全程留痕。

## 二、快速开始

环境要求：Python 3.10+。建议使用虚拟环境。

```bash
# 1) 安装依赖
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2) 初始化数据库（建表 + 同步 config/sources.yaml 中的来源）
python -m policy_collector.cli init-db

# 3) 离线演示：不联网、无需模型 Key，解析 samples/policies 下样例并入库
python -m policy_collector.cli demo

# 4) 查询入库结果
python -m policy_collector.cli query --keyword 投资
python -m policy_collector.cli stats
```

### Web 管理界面（推荐日常使用）

命令行之外提供一个本地 Web 界面（仪表盘 / 政策库检索 / 详情与人工复核 / 一键运行采集），比敲命令直观：

```bash
python -m policy_collector.cli web --open        # 自动打开浏览器 http://127.0.0.1:8000
# 或指定端口：--port 8080；仅监听本机 127.0.0.1
```

界面能力与命令行一一对应：

| 页面 | 能力 | 对应 CLI |
|---|---|---|
| 仪表盘 | 政策数/待复核数/来源数/最近入库与运行 | `stats` |
| 政策库 | 关键词/类别/地区/审核状态筛选，分页 | `query` |
| 政策详情 | 元信息/判断理由/正文/附件 URL/版本历史 | — |
| 人工复核 | 确认采纳 / 调整分类(多选) / 剔除 | `audit` |
| 来源与运行 | 查看各站点状态，一键后台运行采集 | `run --source` |
| 运行日志 | 每批发现/入库/重复/失败统计，运行中自动刷新 | `run_logs` |

「来源与运行」页点击站点「运行」即后台执行 发现→下载→解析→分类→去重→入库，规则模式离线可用；运行中页面每 6 秒自动刷新，完成后回到仪表盘核对新增。

演示运行后数据库落在 `data/policy.db`，样例网页原件已解析入库，可继续执行：

```bash
# 待复核清单（规则分类命中边界样例/多类冲突时产生）
python -m policy_collector.cli query --review pending

# 人工复核：确认采纳 / 调整分类 / 剔除
python -m policy_collector.cli audit --id 1 --action confirm
python -m policy_collector.cli audit --id 5 --action adjust --category guarantee,incentive
python -m policy_collector.cli audit --id 3 --action reject

# 导出
python -m policy_collector.cli export --format csv --out policies.csv
```

### 接入真实官网（v0.1 适配状态）

已按真实页面结构适配并实测可抓：**国家发展改革委**通知（`ndrc_zcwj`）与规范性文件（`ndrc_ghxwj`）栏目——列表页静态直出 `ul.u-list`，详情页正文容器 `div.TRS_Editor`，文号/发文日期/附件（PDF/OFD）自动登记。编辑 `config/sources.yaml` 后执行：

```bash
python -m policy_collector.cli init-db            # 重新同步来源
python -m policy_collector.cli run --source ndrc_zcwj --prefer rule
python -m policy_collector.cli query
```

**浙江省发展改革委**为二期：新版栏目列表迁入省政务公开平台 xxgk 异步接口（带 token 的 JS 调用），列表页无法静态直出；详情页 `art_*.html` 为静态可直接解析。接入该接口后再启用 `zjfgw_gsgg`。政府网站改版会导致选择器失效，届时以 `samples/gov/` 快照 + 官网现网结构更新适配器即可。

- `--prefer rule`：使用内置关键词规则分类（离线可跑）；
- `--prefer llm`：启用大模型分类，需在 `config/config.yaml` 打开 `llm.enabled` 并设置环境变量 `LLM_API_KEY`（与 `LLM_MODEL`）；模型不可用时自动回退规则分类，流程不中断。

### 定时运行

```bash
# 前台按间隔轮询（演示/常驻）
python -m policy_collector.cli schedule --source ndrc_zcwj --interval 60 --prefer rule
```

生产建议用 cron / systemd timer 周期调用 `python -m policy_collector.cli run --source ...`。

## 三、模块划分

```
policy-collector/
├── config/                        # ① 配置层
│   ├── config.yaml                #     运行参数（数据目录/模型/采集行为）
│   ├── sources.yaml               #     采集来源（站点/栏目/过滤规则）
│   └── classification.yaml        #     分类口径（四类关键词/边界样例/文件类型）
├── samples/                       # 离线演示与测试数据
│   ├── list.html                  #     模拟栏目列表页（discover 链路）
│   ├── policies/*.html            #     五份不同特征的政策样例
│   └── gov/*.html                 #     官网真实页面快照（NDRC，适配器离线测试用）
├── policy_collector/
│   ├── config.py                  # 配置加载（YAML→dataclass，支持环境变量覆盖）
│   ├── models.py                  # 领域模型（Document/Classification/IngestResult）
│   ├── db.py                      # ② 数据落地：SQLite 五类记录 + DAO（集中封装便于换库）
│   ├── collector.py               # ③ 采集：网页/附件下载、URL 过滤、列表链接发现
│   ├── site_adapters.py           #   站点适配注册点：列表解析 + 详情页解析（NDRC 等）
│   ├── parser.py                  # ④ 解析：HTML/PDF/Word → 结构化文档与元信息
│   ├── classifier.py              # ⑤ 识别分类：规则 + LLM 双轨（先判"收不收"再判"哪类"）
│   ├── llm_client.py              #   OpenAI 兼容模型客户端（统一 JSON 输出）
│   ├── dedup.py                   # ⑥ 去重与版本管理（URL/转载/更新/疑似重复分层处理）
│   ├── pipeline.py                #   流程编排：采集→解析→分类→去重→入库
│   ├── scheduler.py               # ⑦ 定时运行（轮询式，可 Ctrl+C 停止）
│   ├── webapp.py                  #   本地 Web 管理界面（Flask）
│   ├── templates/ static/         #   界面模板与样式
│   └── cli.py                     #   命令行入口（…/web 等 13 个子命令）
├── data/                          # 运行时生成（policy.db、downloads/ 原件落盘）
├── tests/                         # 冒烟测试（demo 全链路 / NDRC 适配器 / Web 路由）
├── requirements.txt
└── README.md
```

各模块职责单一、相互独立：采集模块不知道分类规则，分类模块不碰数据库，去重只做判断不做写入——方便发现问题后只重跑失败环节，也便于汇报时逐模块讲清思路。

## 四、数据库设计（五类记录）

SQLite（`data/policy.db`），访问全部集中在 `db.py`，后续可平移 PostgreSQL。

| 记录 | 表 | 关键字段 | 用途 |
|---|---|---|---|
| 来源配置 | `source_configs` | name/site/region/list_url/选择器/过滤/最近检查 | 收录边界、站点管理 |
| 采集记录 | `fetch_records` | page_url/status/error/sha256/raw_path/时间 | 每篇 URL 从发现到处理的状态机，失败留待重跑 |
| 政策及版本 | `policies` | policy_key/version/标题/文号/机关/日期/分类/审核状态/判断理由 | 去重主键 + 版本管理 |
| 附件记录 | `attachments` | policy_id/URL/本地路径/格式/校验值/解析状态 | 原件可追溯（初版预留，入库侧已留字段） |
| 运行与审核 | `run_logs` + 审核字段 | run_id/批统计/模型版本/note | 每批可复盘、人工纠正可追踪 |

## 五、分类口径（重要）

规则配置集中在 `config/classification.yaml`，规则分类与 LLM 提示词共用同一口径。

**判断顺序**：先判"是否投资项目政策（yes/no/pending）"，再判四类；判断依据是**条款的实际作用**而非标题关键词。

「收不收」的三条路（v0.1 起）：标题命中排除类型（会议/人事/任免/采购/招标/解读）→ **不收**；命中投资关键词 ≥2 或属申报通知/项目批复 → **收**；正式政策/征求意见稿但正文未命中关键词（如印发通知正文短、实施内容在附件）→ **判 pending 入库交人工复核**，不武断剔除；其余（无法归类的弱信号内容）→ 不收。

| 类别 | 判断重点 | 关键词示例 |
|---|---|---|
| 引导类 guide | 明确投资方向、产业布局、鼓励领域 | 重点领域、鼓励发展、产业布局、引导社会资本 |
| 准入类 access | 项目能否进入及审批核准备案前置条件 | 核准、备案管理、前置条件、负面清单 |
| 保障类 guarantee | 土地、资金、信贷等要素配置支持 | 要素保障、用地保障、资金支持、专项债券 |
| 激励约束类 incentive | 奖补优惠、监管、绩效评价、责任要求 | 奖补、补贴、绩效评价、惩戒、退出机制 |

**易混样例（写进规则，命中即待复核）**：同一"能耗"条款——规定项目能耗准入条件属**准入类**，安排能耗指标保障属**保障类**；"细则"类文件多类混杂默认待复核。

**审核状态机**：自动入库 → `pending`（需复核）/ `confirmed_auto`（规则明确）→ 人工 `confirm / adjust / reject`。模型自报置信度仅供参考，不因置信度高即自动"已确认"。

## 六、去重与版本策略

| 情形 | 处理 |
|---|---|
| 同一 URL、内容未变 | 跳过，仅刷新检查时间 |
| 不同 URL、同一政策（转载） | 不重复入库，保留多来源关联 |
| 同一政策、内容更新 | `policy_key` 相同但 hash 不同 → 追加新版本 v+1 |
| 标题相似、内容不同 | 疑似重复，标记待复核，不自动合并 |

`policy_key` 生成：优先文号，其次归一化标题。

## 七、演示样例说明（samples/policies）

| 文件 | 特征 | 预期处理结果 |
|---|---|---|
| ndrc_reits_2026.html | 基础设施 REITs 申报通知（发改投资〔2026〕312号） | 入库，多类混杂→待复核 |
| zj_fund_2026.html | 战略性新兴产业基金管理办法 | 入库，四类全命中→待复核 |
| zj_major_projects_2025.html | 重大项目前期工作管理办法 | 入库，准入类为主→自动确认 |
| reits_repost_zj.html | 第 1 份的转载 | 内容/文号相同 → duplicate_skip |
| zj_energy_review_2026.html | 节能审查实施细则 | 入库，命中边界→待复核 |
| meeting_notice_2026.html | 会议通知 | 非投资政策 → excluded（不入库） |

> 规则分类结果仅供参考，展示的是"流程能闭环、口径能解释"；正式验收需按业务口径人工校准样本并统计准确率/漏报率。

## 八、验收对照（与任务要求对应）

| 任务点 | 本系统对应实现 | 如何验证 |
|---|---|---|
| 覆盖国家、浙江及外省发改委官网 | `config/sources.yaml` 多来源；NDRC 两栏目已实测，浙江 xxgk 二期 | 扩展站点 = 新增一条配置 +（必要时）详情适配器 |
| 识别投资项目政策 | relevance 关键词 + LLM 判定（是/否/待复核） | 样本人核对准率 |
| 按四类归集 | `classification.yaml` 口径，规则/LLM 双轨 | 分类别统计，重点查保障/激励混淆 |
| 提取标题/正文/日期/文号/附件 | parser 抽取 + 元信息正则；页面挂载附件登记 URL | 与原文人工比对 |
| 去重入库 | dedup 四层策略 + policy_key/版本 | 重复运行不新增记录 |
| 定期采集 | scheduler / cron | 保留 ≥2 次真实运行记录 + 演示失败补跑 |
| 失败可恢复 | fetch_records 状态机，失败项留待重跑 | 断网模拟后重跑成功项不重复、失败项补上 |

## 九、后续规划（初版范围外）

- **浙江省发展改革委 xxgk 异步接口适配**（列表需 token，详情已静态可解）并启用 `zjfgw_gsgg`；
- 附件下载与正文解析接通（PDF/Word 附件已登记 URL 到 `attachments` 表，解析失败保留原件进入补处理队列）；
- 更多官网站点适配（站点专属列表/详情解析见 `site_adapters.py` 两个注册点）；
- 增量更新检测（对比栏目近期文件，防"补发/修订"漏采）；
- 检索增强：入库后接全文检索/向量检索，支撑研究报告中的"政策检索与研究辅助"场景；
- Web 界面增强：批量复核、导出按钮、来源启停开关。

## 十、说明

- 本系统为**实习任务初版工程**，用于演示与验收；生产部署需补充鉴权、日志集中、模型调用计费等。
- 政策是否"现行有效"缺少依据时标 `pending`，由人工确认，不自动推断。
- 判定"收不收、分哪类"是业务口径问题，最终以指导员确认的收录规则为准。
