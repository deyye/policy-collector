# 真实模型分类评测报告（规则 vs DeepSeek）

> 评测对象：`gold/zj_v1_20260909/gold.jsonl`（56 条真实浙江政策人工标注）
> 模型：`deepseek-v4-flash`（provider=custom，`https://api.deepseek.com`）
> 日期：2026-09-10　执行：`scripts/eval_llm_compare.py`　耗时：9m16s（56 条，串行）

## 一、结论摘要

**LLM 分类全面优于规则**，尤其在相关性判断上：relevance 精确率 0.619→**0.865**、召回率 0.703→**0.865**，exact_match 0.089→**0.482（5.4 倍）**。规则分类器把约 38% 的非投资政策误收（大量定价/收费文件命中"监管"关键词），LLM 把误收压到约 13.5%。

**但 LLM 存在一个系统性偏差需修正**：类别标注**过宽**——56 条中 **22 条出现"多标"**（把监管/考核/问责/一般审批流程也归入 incentive 或 access），导致 category 精确率仅 0.533。这是下一轮提示词优化的首要目标。

## 二、指标对比（同一份 gold，同口径）

| 指标 | 规则（rule） | LLM（deepseek-v4-flash） | 变化 |
|---|---|---|---|
| relevance 精确率 | 0.6190 | **0.8649** | +0.246 |
| relevance 召回率 | 0.7027 | **0.8649** | +0.162 |
| category 微精确率 | 0.3733 | **0.5333** | +0.160 |
| category 微召回率 | 0.5714 | **0.8163** | +0.245 |
| **exact_match** | 0.0893 | **0.4821** | **×5.4** |
| pending（未判定） | 14 | **6** | −8 |
| rule_fallback | 0 | 0 | — |

**口径说明（重要）**：与 `scripts/evaluate.py` 一致，`pending` 视为**未命中**——若 gold 为相关而预测为 pending，计入 FN（保守口径）。因此上述数字是"把 pending 当错误"的下限值。若剔除 pending、只看已判定的 50 条，LLM 的 relevance 精确率/召回率为 **0.865 / 0.970**。

## 三、LLM 混淆矩阵（56 条）

| | gold 相关 | gold 非相关 | 合计 |
|---|---|---|---|
| LLM 判 yes | 32 (TP) | 5 (FP) | 37 |
| LLM 判 no | 1 (FN) | 12 (TN) | 13 |
| LLM pending | 4 | 2 | 6 |
| 合计 | 37 | 19 | 56 |

**类别召回**（仅 37 条相关样本）：access 16/18 = 0.89　guide 10/12 = 0.83　guarantee 8/10 = 0.80　**incentive 6/9 = 0.67（最弱）**

## 四、错误分析

### 4.1 FP·误收 5 条（LLM 判相关、人工判非相关）

| id | 标题 | LLM 判定 | 分歧点 |
|---|---|---|---|
| 10 | 能源领域工程建设项目招投标监管指导意见 | incentive | 招投标**程序监管**，不改变投资决策 |
| 28 | 清理减免规范工程建设项目投标保证金 | incentive | 保证金**减负**，属交易成本而非项目投资 |
| 45 | 促进服务业领域困难行业恢复发展政策意见 | guarantee,incentive | **经营纾困**（税费减免/房租/信贷）而非项目投资 |
| 223 | 企业债券发行中公开选定中介机构 | access,incentive | 发债**程序廉洁**要求 |
| 240 | 行政管理事项中应用信用记录和信用报告 | access,incentive | 信用工具**通用应用**，非投资专项 |

> 这 5 条是**口径分歧**而非纯粹错误——LLM 依据"条款是否含准入/监管/激励要素"判定，人工依据"是否作用于投资项目决策"。**建议人工复核裁决**：若认可 LLM 口径，需同步放宽 gold；若坚持严格口径，则需在提示词中明确"监管程序类不计入"。

### 4.2 FN·漏判 1 条

| id | 标题 | 人工 | LLM | 说明 |
|---|---|---|---|---|
| 100 | 天然气上下游直接交易暨管网代输试点规则 | access（★边界） | no | gold 本身已标为边界难例，LLM 认为管网开放规则不属于投资政策。**属真实分歧，建议复核** |

### 4.3 多标·类别过宽 22 条（最值得修，且暴露了口径本身的歧义）

典型样例：

| id | gold 类别 | LLM 输出 | 多出 |
|---|---|---|---|
| 118 | guide | guide,access,guarantee,incentive | access,guarantee,incentive |
| 93 | guide | 全部四类 | access,guarantee,incentive |
| 105 | guarantee,incentive | 全部四类 | guide,access |
| 178 | access,guarantee | 全部四类 | guide,incentive |
| 192 | guide | guide,guarantee,incentive | guarantee,incentive |

**根因（重要，指向口径歧义而非模型错误）**：

现行 `classification.yaml` 与 [README 口径第 2 条](README.md)把 `incentive` 定义为
"奖补/补贴/电价等激励 **+ 监管/考核/惩戒等约束**"，把 `access` 定义为"核准备案/前置条件/管理办法"。
LLM **严格按该定义执行**——凡含监督、绩效、问责、信用惩戒条款的一律计入 `incentive`，
凡含审批/申报流程的一律计入 `access`。

而人工标注实践中口径更**狭窄**：只有当条款含**直接经济激励**（奖补/补贴/价格/税收/差别化电价）
或**明确的项目准入门槛**时才标注，一般管理性条款不计入。

**结论：这是"文字口径"与"标注实践"之间的不一致——同一套定义下，模型与人工的行为不同，
说明 `incentive` / `access` 的边界定义存在歧义，需要业务方裁决。**

**修正方向（两条路，择一）**：

- **路线 A（收窄模型）**：在提示词中显式约束——`incentive` 不含"监管/考核/问责/信用惩戒"，
  除非该条款伴随具体奖惩性经济措施；`access` 仅限"投资项目准入"，排除一般行政程序。
  同时同步修改 `classification.yaml` 中 `incentive` 的定义文字。
- **路线 B（放宽金标准）**：若业务上认可"约束类条款也算 incentive"，则应修订 gold 标注
  （对涉及监管/考核的样本补标 incentive），使口径与实践一致。

预计按路线 A 调整后，category 精确率（当前 0.533）将有明显提升空间。

## 五、复现与数据

```bash
# 全量对比（需先配置真实模型）
python scripts/eval_llm_compare.py \
    --src-db /tmp/pc_zj_full/policy.db \
    --gold gold/zj_v1_20260909/gold.jsonl \
    --out-dir /tmp/pc_llm_eval

# 单跑规则基线
python scripts/evaluate.py --db /tmp/pc_zj_full/policy.db \
    --gold gold/zj_v1_20260909/gold.jsonl
```

产物：`report.json`（指标）、`reclassify_diffs.jsonl`（变更记录）、本目录 `llm_predictions.jsonl`（56 条三方对照：人工/规则/LLM，含判定结果与类别差异）。

## 六、结论与建议

1. **可以宣称"真实模型已验证"**：56 条全量跑通、0 失败、0 回退，指标可复现。
2. **LLM 相关性判断已达可用水平**（精确率 0.865），可用于替代规则做初筛，配合 `need_review` 人工复核。
3. **类别标注的边界需先统一口径**（精确率 0.533，22 条多标源于口径歧义，见 §4.3），而非简单归咎模型；建议业务方先在两条路线中择一。
4. **建议下一步**：① 裁决 §4.1 的 5 条 relevance 口径分歧；② 就 §4.3 的 incentive/access 边界择定路线 A 或 B；③ 复跑本评测，观察 category 精确率提升幅度。
