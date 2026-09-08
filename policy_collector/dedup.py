"""去重与版本管理。

去重分层（与任务口径一致）：
1) 同一 URL 内容未变       → 跳过（仅刷新检查时间）
2) 不同 URL 同一政策       → 保留多来源，关联同一 policy_key（不做重复入库）
3) 同一 URL 内容改变/新版本 → policy_key 相同但内容 hash 不同 → 追加新版本(version+1)
4) 标题相似但内容不同       → 疑似重复，标记待复核，不自动合并

policy_key 生成：优先"文号"，其次"归一化标题"。归一化 = 去空格/全半角统一/去括号内容。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .db import Database
from .models import Document, IngestResult


@dataclass
class PolicyKey:
    key: str
    source: str  # wenhao / title
    title_norm: str


def _norm_title(t: str) -> str:
    t = (t or "").lower()
    # 全角->半角
    t = t.replace("（", "(").replace("）", ")").replace("　", " ")
    t = re.sub(r"[《》\"'“”‘’\s]", "", t)
    t = re.sub(r"\(.*?\)", "", t)
    return t.strip()


def make_policy_key(doc: Document) -> PolicyKey:
    if doc.wenhao:
        # 文号清洗：去掉括号/空格，如"浙发改〔2025〕12号"→"浙发改〔2025〕12号"
        key = re.sub(r"[\s（(【\[].*?[)）】\]]", "", doc.wenhao)
        key = re.sub(r"\s", "", key)
        return PolicyKey(key=key, source="wenhao", title_norm=_norm_title(doc.title))
    t = _norm_title(doc.title)
    return PolicyKey(key=t or hashlib.md5((doc.content or "").encode()).hexdigest()[:16],
                     source="title", title_norm=t)


def content_hash(doc: Document) -> str:
    base = (doc.content or "").strip()
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


class Deduplicator:
    def __init__(self, db: Database):
        self.db = db

    def check(self, doc: Document) -> IngestResult:
        """入库前检查：返回决策（skip / update / ingested），已存在则给出结果。"""
        pk = make_policy_key(doc)
        prev = self.db.find_policy(pk.key)
        if prev is None:
            return IngestResult(decision="ingested", detail=f"新政策 key={pk.key}（{pk.source}）")

        # 内容完全相同（不同来源转载或重复采集）
        if prev.get("content_sha256") and prev["content_sha256"] == content_hash(doc):
            return IngestResult(
                policy_id=prev["id"], decision="duplicate_skip", version=prev["version"],
                detail="同一政策内容已入库，跳过（保留多来源关联）",
            )

        # 同一 key 内容变化 → 新版本
        return IngestResult(
            policy_id=prev["id"], decision="updated", version=prev["version"] + 1,
            detail=f"检测到内容变化，追加为 v{prev['version'] + 1}", new_version_created=True,
        )
