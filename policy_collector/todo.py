"""待办类型：把混装的 `need_review` 拆成"谁能解决"。

**为什么需要这个模块**

`need_review` 是个垃圾桶——材料缺件、模型故障、结论待定三类问题全塞在同一个布尔里。
实测当前库 401 条里有 395 条 `pending`，页面上只剩一个没有行动指向的数字。
拆开之后每类对应一个明确责任方：

| 类型 | 责任方 | 含义 |
|---|---|---|
| `material` | **机器自修** | 附件没下全或读不出。补齐后自动重判，**不该占人的时间** |
| `system` | **运维处理** | 模型接口不可用导致的回退。接口配好后会自动补判 |
| `review` | **业务判断** | 机器判不下去，需要人看——这才是真正的人工待办 |
| `none` | 无需处理 | 判定明确 |

**为什么是"派生"而不是"新增列"**

另一条并行分支（`feat/scope-criteria-and-acceptance`）把这件事做成了 `todo_type` 列。
但两条线的 `policies` 表已经分叉（本分支 37 列 / 那条 40 列），加列的合并代价高于派生。
本模块集中放派生逻辑，界面与补材料流程都从这里取，避免两处各写一套。

**SQL 版与 Python 版必须一致**（`sql_case()` 与 `derive()`）——
测试 `test_todo_derivations_agree` 会在真实库上逐条比对两者，不一致就报错。
"""
from __future__ import annotations

MATERIAL = "material"
SYSTEM = "system"
REVIEW = "review"
NONE = "none"

# 展示顺序：先机器自修、再运维、最后才是人；"无需处理"不进待办清单
TODO_ORDER = (MATERIAL, SYSTEM, REVIEW)

#: 类型 → (名称, 责任方, 一句话说明)
TODO_META = {
    MATERIAL: ("材料待补", "机器自修", "附件没下全或读不出，补齐后自动重判"),
    SYSTEM: ("系统待修", "运维处理", "模型接口不可用，配好后自动补判"),
    REVIEW: ("结论待确认", "业务判断", "机器判不下去，需要人看一眼"),
    NONE: ("无需处理", "—", "判定明确，不需要人工"),
}


def missing_text(attachments) -> bool:
    """附件里是否存在**拿不到正文**的。"""
    return any(not (a.get("parsed_text") or "").strip() for a in attachments or [])


def document_incomplete(doc) -> bool:
    """文档材料是否不完整：正文解析失败，或任一附件拿不到正文。

    **这是全仓库唯一的"材料不完整"判据。** 此前这句话被四处各写一遍，
    且都写成 `parse_status != 'ok'`——于是 `partial`（文本已提取、只是转换过程
    有提示，例如旧版 doc 经 textutil 转换）被当成缺失，可用材料永远堵在待办队列里。
    本项目已因此踩过三次坑（附件状态计数、正文容器、如今是 parse_status 本身），
    所以收敛到这里，新增判据一律复用，不要再手写一遍。
    """
    return bool(getattr(doc, "parse_error", "")) or missing_text(getattr(doc, "attachments", []))


def derive(policy: dict, attachments: list[dict] | None = None) -> str:
    """派生单条政策的待办类型（与 `sql_case()` 的语义必须一致）。

    判定顺序即优先级：材料不全最优先（机器能修的先修，修完结论才有意义），
    其次是模型故障，最后才是"真需要人判断"。

    **材料判据是"有没有拿到正文"，不是 `parse_status` 是否恰好为 `ok`**：
    `partial` 表示已提取出文本、只是转换过程有提示（例如旧版 doc 经 textutil 转换），
    把它算成缺失会让**可用材料永远堵在待办队列里**——本项目已因此踩过三次坑。
    """
    if (policy.get("parse_error") or "").strip():
        return MATERIAL
    if missing_text(attachments):
        return MATERIAL
    # 刻意**不看 `parse_requires_review`**：它是个历史包袱——旧代码在任一附件
    # `parse_status != 'ok'` 时就会置位，于是"附件是 partial 但正文完整"也被标上，
    # 实测当前库里 78 条"材料待补"有 46 条属于这种情况（材料其实是好的）。
    # 材料层缺没缺东西，看"有没有正文"就够，这个字段不参与派生。
    if (policy.get("fallback_reason") or "").strip():
        return SYSTEM
    if policy.get("need_review"):
        return REVIEW
    return NONE


def sql_case(policy_alias: str = "p", att_alias: str = "a") -> str:
    """派生待办类型的 SQL 表达式。语义与 `derive()` 一一对应。

    材料判据用 EXISTS 子查询查 `attachments` 里**有没有正文**，而不是匹配
    `reviewer_hint` 的文本——文本会随文案改动而失效，附件状态是事实。
    同样不用 `parse_status != 'ok'`：`partial` 已有文本，不算缺件。
    """
    p, a = policy_alias, att_alias
    return (
        "CASE "
        f"WHEN COALESCE({p}.parse_error,'') != '' THEN '{MATERIAL}' "
        f"WHEN EXISTS (SELECT 1 FROM attachments {a} WHERE {a}.policy_id = {p}.id "
        f"AND COALESCE({a}.parsed_text,'') = '') THEN '{MATERIAL}' "
        f"WHEN COALESCE({p}.fallback_reason,'') != '' THEN '{SYSTEM}' "
        f"WHEN COALESCE({p}.need_review,0) = 1 THEN '{REVIEW}' "
        f"ELSE '{NONE}' END"
    )


def is_stale_conclusion(policy: dict, attachments: list[dict] | None = None) -> bool:
    """这份政策的结论是否已过期（还挂在"材料待补"里）。

    补材料流程用它判断要不要重判：**只重判过期的**，而不是每次都重判。
    这样天然幂等——重判成功后它就不再属于 material，下次不会重复触发；
    反过来，如果按"补完材料一律重判"，每次 repair 都会把所有条目再跑一遍模型。
    """
    return derive(policy, attachments) == MATERIAL
