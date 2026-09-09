# 浙江政策分类标注集 v1（zj_v1_20260909）

P0-1「真实模型分类评测」的人工金标准样本集。共 **56 条**真实浙江省发改委政策，
从浙江隔离库（`zj_full_ingest_check.py` 双轮全量入库产物，387 条候选 / 323 条入库）分层抽取，
逐条阅读正文后人工标注。

## 文件清单

| 文件 | 内容 |
|---|---|
| `gold.jsonl` | **评测入口**。每行 `{id, relevant, categories, title, wenhao, hard, note}`，可直接喂给 `scripts/evaluate.py --gold` |
| `review_workbook.md` | 标注工作簿：56 条全部字段对照表（含系统预测 vs 人工标注），人工复核用 |
| `policies_snapshot.jsonl` | 每条样本的原文快照（标题/文号/URL/正文前 600 字），标注依据可追溯 |
| `README.md` | 本说明 |

## 数据来源

- 采样库：浙江发改委「行政规范性文件」栏目全量入库隔离库（`POLICY_DATA_DIR=/tmp/pc_zj_full`，387 条候选 → 323 条入库，2026-09-09 双轮验证产物）。
- 该库 policy `id` 即 `gold.jsonl` 的 `id`，评测时用 `evaluate.py --db <该库路径> --gold gold.jsonl`。
- 如需重建采样库：`python scripts/zj_full_ingest_check.py --data-dir <目录> --rounds 2 --limit 600`（约 10 分钟）。

## 分布设计

目标 40-60 条，覆盖"四类 + 非相关 + 边界"。实际 56 条：

| 维度 | 数量 | 说明 |
|---|---|---|
| 相关（relevant=true） | 37 | 判定为投资相关政策 |
| 非相关（relevant=false） | 19 | 系统多判 yes、人工判否（FP 探测样本） |
| guide 引导类 | 12 | 含多标签样本 |
| access 准入类 | 18 | 含多标签样本 |
| guarantee 保障类 | 10 | 含多标签样本 |
| incentive 激励约束类 | 9 | 含多标签样本 |
| 边界难例（hard） | 16 | 多类交叉/细则类/易混型，标注置信度低 |

类别分布可多标签叠加（如"设备更新"标 guide+incentive+guarantee），与
`classification.yaml` 的 `boundary_cases` 口径一致。

## 标注口径（与 classification.yaml 对齐）

1. **先判相关，再判类**：是否"投资项目相关政策"（涉及投资方向引导/项目核准备案准入/要素资金保障/奖补激励约束），非相关则 categories 为空。
2. **按条款实际作用判类，不按标题一刀切**：
   - guide = 引导投资方向/产业布局/鼓励领域；
   - access = 核准备案/前置条件/管理办法/负面清单等准入安排；
   - guarantee = 土地/资金/信贷/要素及资源配置保障；
   - incentive = 奖补/补贴/电价等激励 + 监管/考核/惩戒等约束。
3. **易混类型**（已按此处理）：
   - 定价/收费/成本监审类（学费、门票、物业、公墓等）→ 民生价格管理，**非投资政策**；
   - 行政规范性文件清理/废止目录 → 机关法治事务，**非投资**；
   - 招投标**程序**监管（评标专家库、招投标监管细则）→ 非投资政策；但**涉及项目准入/社会资本进入**的（如 PPP、社会资本招标）→ 相关；
   - 信用评价/信用应用类 → 工具型制度，非投资政策；
   - 排污权、天然气管网开放 → 涉及项目要素/准入，判相关（hard 边界）。

## 使用

```bash
# 评测（db 指向标注样本来源的隔离库）
python scripts/evaluate.py --db /tmp/pc_zj_full/policy.db --gold gold/zj_v1_20260909/gold.jsonl
```

输出 relevance 精确率/召回率、category micro 精确率/召回率、exact_match、pending/rule_fallback 占比。

## 复核提示

本 v1 标注由 AI 按上述口径初标，**未经业务人员终审**。建议人工复核顺序：
1. 先看 `review_workbook.md` 里 16 条 `★` 边界样本（最易有分歧）；
2. 再快速扫非相关 19 条（若认为某条其实相关，改 relevant 并补 categories）；
3. 修改 `gold.jsonl` 后重新评测即可。
