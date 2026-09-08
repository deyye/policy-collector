"""流程编排 Pipeline：把 采集->解析->分类->去重->入库 串起来。

每批运行生成 run_id 写 run_logs；单篇失败不中断整批；
失败/待复核项可单独重跑。全流程可在 offline demo 模式运行。
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .classifier import Classifier
from .collector import CandidateLink, Collector, ListPageParser
from .config import AppConfig, SourceConfig
from .db import Database
from .dedup import Deduplicator, content_hash, make_policy_key
from .models import Classification, Document, now
from .parser import Parser


@dataclass
class RunStats:
    discovered: int = 0
    downloaded: int = 0
    parsed: int = 0
    ingested: int = 0
    updated: int = 0
    duplicates: int = 0
    failed: int = 0
    need_review: int = 0
    excluded: int = 0

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def __add__(self, other: "RunStats") -> "RunStats":
        merged = RunStats()
        for k in merged.to_dict():
            setattr(merged, k, getattr(self, k) + getattr(other, k))
        return merged


class Pipeline:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.collector = Collector(cfg)
        self.parser = Parser()
        self.classifier = Classifier(cfg)
        self.dedup = Deduplicator(self.db)

    # ---------- 基础设施 ----------
    def _fmt(self, url: str) -> str:
        """从 URL 推断文件类型，用于 Parser 分派。"""
        if url.startswith("file://"):
            return Path(url.replace("file://", "")).suffix.lstrip(".") or "html"
        path = re.split(r"[?#]", url)[0].lower()
        for ext in (".pdf", ".docx", ".doc", ".shtml", ".htm", ".html"):
            if path.endswith(ext):
                return ext.lstrip(".")
        return "html"

    def _fetch_id_for_url(self, source_id: int, url: str) -> Optional[int]:
        for r in self.db.list_fetch(source_id=source_id, limit=10000):
            if r["page_url"] == url:
                return r["id"]
        return None

    # ---------- 模块一：来源同步 ----------
    def sync_sources(self) -> int:
        """把 config/sources.yaml 的来源同步进库（幂等）。返回来源数。"""
        n = 0
        for name, src in self.cfg.sources.items():
            self.db.upsert_source(
                name=name,
                site=src.site, region=src.region, category=src.category,
                enabled=int(src.enabled), list_url=src.list_url,
                link_selector=src.link_selector, max_pages=src.max_pages,
            )
            n += 1
        return n

    # ---------- 模块二：列表发现 ----------
    def discover(self, source: SourceConfig, run_id: str) -> RunStats:
        stats = RunStats()
        row = self.db.get_source(source.name)
        if not row:
            return stats
        sid = row["id"]
        links: list[CandidateLink] = []
        try:
            from .site_adapters import get_list_parser
            custom_parser = get_list_parser(source.name)  # 站点专属适配器（扩展点）

            if source.list_url.startswith("file://"):
                p = Path(source.list_url.replace("file://", ""))
                if not p.is_absolute():
                    p = Path(self.cfg.data_dir.parent) / p
                p = p.resolve()
                html = p.read_text(encoding="utf-8")
                base_uri = p.as_uri()  # file:///.../list.html，供相对链接拼接
                if custom_parser:
                    from bs4 import BeautifulSoup
                    links = custom_parser(BeautifulSoup(html, "lxml"), source)
                else:
                    links = ListPageParser(source).parse(html, base_url=base_uri)
            else:
                pages = range(1, source.max_pages + 1)
                for page in pages:
                    url = source.list_url.format(page=page) if "{page}" in source.list_url else source.list_url
                    res = self.collector.fetch(url)
                    if not res.ok:
                        stats.failed += 1
                        continue
                    html = res.content.decode("utf-8", "ignore")
                    if custom_parser:
                        from bs4 import BeautifulSoup
                        page_links = custom_parser(BeautifulSoup(html, "lxml"), source)
                    else:
                        page_links = ListPageParser(source).parse(html, base_url=url)
                    links.extend(page_links)
                    if "{page}" not in source.list_url:
                        break

            # 过滤 + 落库（discovered 状态）
            existing = {r["page_url"] for r in self.db.list_fetch(source_id=sid, limit=10000)}
            for lnk in links:
                if lnk.url in existing:
                    continue
                if not self.collector.is_policy_url(lnk.url, source):
                    stats.excluded += 1
                    continue
                existing.add(lnk.url)
                self.db.add_fetch(sid, lnk.url, status="discovered", title=lnk.title)
                stats.discovered += 1
            self.db.touch_source(sid)
        except Exception as e:  # noqa: BLE001
            self.db.finish_run(run_id, stats.to_dict(), status="failed", note=f"discover: {e}")
        return stats

    # ---------- 单篇入库（模块三~七） ----------
    def ingest_url(self, source: SourceConfig, url: str, raw: Optional[bytes] = None,
                   prefer: str = "llm") -> RunStats:
        """下载→解析→分类→去重→入库 单篇。返回本单统计。"""
        stats = RunStats()
        row = self.db.get_source(source.name)
        if not row:
            raise KeyError(f"来源未同步进库: {source.name}")
        sid = row["id"]
        fid = self._fetch_id_for_url(sid, url)
        if fid is None:
            fid = self.db.add_fetch(sid, url, status="downloading")

        # 1. 下载
        if raw is None:
            if url.startswith("file://"):
                p = Path(url.replace("file://", ""))
                if not p.is_absolute():
                    p = Path(self.cfg.data_dir.parent) / p
                if not p.exists():
                    stats.failed += 1
                    self.db.update_fetch(fid, status="failed", error=f"本地文件不存在: {p}")
                    return stats
                raw = p.read_bytes()
            else:
                res = self.collector.fetch(url)
                if not res.ok:
                    stats.failed += 1
                    self.db.update_fetch(fid, status="failed", error=res.error)
                    return stats
                raw = res.content
        fmt = self._fmt(url)
        if not url.startswith("file://") and raw:
            self.collector.save(source.name, url, raw, fmt)
        self.db.update_fetch(fid, status="downloaded", downloaded_at=now())

        # 2. 解析
        origin = url if url.startswith("file://") else ""
        doc: Document = self.parser.parse(raw, fmt, page_url=url, origin_path=origin)
        if not doc.content:
            stats.failed += 1
            self.db.update_fetch(fid, status="failed", error="正文解析为空")
            return stats
        if not doc.title:
            doc.title = (doc.content[:60] or "未命名").replace("\n", " ")
        stats.parsed += 1

        # 3. 去重决策（先判重复，减少不必要的模型/规则调用）
        decision = self.dedup.check(doc)
        if decision.decision == "excluded":
            stats.excluded += 1
            self.db.update_fetch(fid, status="excluded", processed_at=now())
            return stats
        if decision.decision == "duplicate_skip":
            stats.duplicates += 1
            self.db.update_fetch(fid, status="processed", processed_at=now())
            return stats

        # 4. 分类
        cls: Classification = self.classifier.classify(doc, prefer=prefer)
        if cls.need_review:
            stats.need_review += 1

        # 4.1 先判"收不收"：非投资政策（如会议通知/人事任免）不入政策库，只留采集记录
        if cls.is_investment_policy == "no":
            stats.excluded += 1
            self.db.update_fetch(fid, status="excluded", processed_at=now(),
                                 error=("排除：" + cls.reason[:200]))
            return stats

        # 5. 入库（ingested 首版 / updated 追加版本）
        key = make_policy_key(doc).key
        pid, version = self.db.add_policy_version(key, self._policy_row(source, doc, cls, fid))
        # 5.1 页面挂载附件 → 记录 URL（一期只登记不下载；二期下载+解析正文）
        for att in (doc.attachments or []):
            self.db.add_attachment(pid, {
                "name": att.get("name", ""), "url": att.get("url", ""),
                "fmt": att.get("fmt", ""), "parse_status": "not_parsed",
            })
        if decision.decision == "updated":
            stats.updated += 1
        else:
            stats.ingested += 1
        self.db.update_fetch(fid, status="processed", processed_at=now())
        return stats

    def _policy_row(self, source: SourceConfig, doc: Document, cls: Classification, fid: int) -> dict:
        return {
            "title": doc.title,
            "wenhao": doc.wenhao,
            "issuing_authority": doc.issuing_authority,
            "page_date": doc.page_date,
            "doc_date": doc.doc_date,
            "region": source.region,
            "site": source.site,
            "page_url": doc.page_url or "",
            "doc_type": cls.doc_type or doc.doc_type or "其他",
            "category": cls.category,
            "category_names": cls.category_names,
            "is_investment_policy": cls.is_investment_policy,
            "need_review": int(cls.need_review),
            "reason": (cls.reason or "")[:1000],
            "evidence": (cls.evidence or "")[:1000],
            "confidence": cls.confidence,
            "model_version": cls.model_version or "",
            "review_status": "pending" if cls.need_review else "confirmed_auto",
            "content": (doc.content or "")[:80000],
            "content_sha256": content_hash(doc),
            "source_fetch_id": fid,
        }

    # ---------- 整源运行 ----------
    def run_source(self, source_name: str, prefer: str = "llm", limit: int = 50) -> RunStats:
        run_id = f"run-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        src = self.cfg.sources.get(source_name)
        if src is None:
            raise KeyError(f"未知来源: {source_name}")
        row = self.db.get_source(source_name)
        rid = self.db.start_run(run_id, row["id"], kind="manual")

        total = RunStats()
        d_stats = self.discover(src, run_id)   # 发现新链接
        total += d_stats
        pending = self.db.list_fetch(source_id=row["id"], status="discovered", limit=limit)
        if not pending:
            # 无新链接时允许重跑失败的
            pending = self.db.list_fetch(source_id=row["id"], status="failed", limit=limit)
        for fr in pending:
            total += self.ingest_url(src, fr["page_url"], prefer=prefer)
            self.db.update_fetch(fr["id"], processed_at=now())
        self.db.finish_run(run_id, total.to_dict(), status="ok")
        return total

    # ---------- demo：离线闭环演示 ----------
    def run_demo(self, samples_dir: Optional[Path] = None) -> RunStats:
        """无需联网/无 Key：解析 samples/ 下全部 html 并入库，验证全链路。"""
        src = SourceConfig(name="demo_local", site="本地样例", region="样例", category="演示")
        self.db.upsert_source(name=src.name, site=src.site, region=src.region,
                              category=src.category, enabled=1, list_url="", link_selector="", max_pages=1)
        run_id = f"demo-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        row = self.db.get_source(src.name)
        rid = self.db.start_run(run_id, row["id"], kind="demo")
        samples_dir = samples_dir or (Path(self.cfg.data_dir.parent) / "samples" / "policies")
        total = RunStats()
        for f in sorted(samples_dir.glob("*.html")):
            url = f"file://{f}"
            total += self.ingest_url(src, url, prefer="rule")
        total.discovered = total.ingested + total.duplicates + total.failed
        self.db.finish_run(run_id, total.to_dict(), status="ok")
        return total
