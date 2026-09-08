"""采集模块：网页/附件下载 + 列表页候选链接发现。

设计原则：
- 站点差异收敛在 site_adapters.py；默认通用解析（CSS 选择器 + URL 过滤）。
- 下载失败不中断整批：失败项留 fetch_records 待补采队列，重跑只处理失败项。
"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .config import AppConfig, SourceConfig


@dataclass
class FetchResult:
    ok: bool = True
    url: str = ""
    final_url: str = ""
    content: bytes = b""
    content_type: str = ""
    error: str = ""
    sha256: str = ""


@dataclass
class CandidateLink:
    url: str
    title: str = ""


class Collector:
    """负责抓取网页/附件，与 URL 过滤。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg.fetch.user_agent

    # ---------- 抓取 ----------
    def fetch(self, url: str, timeout: Optional[int] = None) -> FetchResult:
        """下载一个 URL（网页或附件）。带重试。"""
        t0 = time.time()
        attempt = 0
        last_err = ""
        while attempt <= self.cfg.fetch.retries:
            try:
                resp = self.session.get(
                    url,
                    timeout=timeout or self.cfg.fetch.timeout_seconds,
                    verify=self.cfg.fetch.verify_ssl,
                    stream=True,
                )
                resp.raise_for_status()
                chunks = []
                size = 0
                for chunk in resp.iter_content(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.cfg.fetch.max_page_bytes:
                        return FetchResult(ok=False, url=url, error="超过单文件大小上限")
                data = b"".join(chunks)
                return FetchResult(
                    ok=True,
                    url=url,
                    final_url=resp.url,
                    content=data,
                    content_type=resp.headers.get("Content-Type", ""),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                attempt += 1
                if attempt <= self.cfg.fetch.retries:
                    time.sleep(self.cfg.fetch.retry_backoff_seconds * attempt)
        return FetchResult(ok=False, url=url, error=last_err)

    # ---------- 落盘 ----------
    def save(self, source_name: str, url: str, content: bytes, ext: str = "html") -> str:
        """按 来源/YYYYMMDD/哈希前12位.ext 落盘，返回相对 data 的路径。"""
        day = time.strftime("%Y%m%d")
        h = hashlib.sha256(content).hexdigest()[:12]
        folder = self.cfg.downloads_dir / source_name / day
        folder.mkdir(parents=True, exist_ok=True)
        safe_ext = ext.lstrip(".").lower() or "bin"
        path = folder / f"{h}.{safe_ext}"
        path.write_bytes(content)
        return str(path.relative_to(self.cfg.data_dir.parent))  # 存相对项目根路径

    def is_policy_url(self, url: str, src: SourceConfig) -> bool:
        """候选链接过滤：include 命中任一 / exclude 命中任一即排除。"""
        if not url.startswith(("http", "file")):
            return False
        if src.include and not any(k in url for k in src.include):
            return False
        if src.exclude and any(k in url for k in src.exclude):
            return False
        # 通用噪音过滤
        noise = re.compile(r"(javascript:|mailto:|#|\.jpg|\.png|\.gif|\.css|\.js$)", re.I)
        return not noise.search(url)


class ListPageParser:
    """从列表页 HTML 中发现候选政策详情链接。"""

    def __init__(self, src: SourceConfig):
        self.src = src

    def parse(self, html: str, base_url: str = "") -> list[CandidateLink]:
        soup = BeautifulSoup(html, "lxml")
        if self.src.link_selector:
            anchors = soup.select(self.src.link_selector)
        else:
            anchors = soup.find_all("a")
        links: list[CandidateLink] = []
        seen: set[str] = set()
        for a in anchors:
            href = (a.get("href") or "").strip()
            if not href:
                continue
            href = urllib.parse.urljoin(base_url or self.src.list_url, href)
            if href in seen:
                continue
            if not self._match(href):
                continue
            title = " ".join(a.get_text(strip=True).split())
            seen.add(href)
            links.append(CandidateLink(url=href, title=title[:200]))
        return links

    def _match(self, url: str) -> bool:
        if url in ("", "#") or not url.startswith(("http", "file")):
            return False
        if self.src.include and not any(k in url for k in self.src.include):
            return False
        if self.src.exclude and any(k in url for k in self.src.exclude):
            return False
        noise = re.compile(r"(javascript:|mailto:|\.jpg$|\.png$|\.gif$|\.css$|\.js$)", re.I)
        return not noise.search(url)
