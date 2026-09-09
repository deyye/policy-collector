"""Bounded HTTP downloads, immutable originals and configured list discovery."""
from __future__ import annotations

import hashlib
import ast
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
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


def normalized_url(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))


def allowed_url(url: str, src: SourceConfig) -> bool:
    p = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlsplit(src.list_url)
    if p.scheme == "file":
        return origin.scheme == "file"
    if p.scheme not in ("http", "https") or not p.hostname or p.username:
        return False
    hosts = src.allowed_hosts or ([origin.hostname] if origin.hostname else [])
    if hosts and p.hostname not in hosts:
        return False
    if src.detail_url_pattern and not re.search(src.detail_url_pattern, url):
        return False
    if src.include and not any(k in url for k in src.include):
        return False
    if src.exclude and any(k in url for k in src.exclude):
        return False
    return not re.search(r"\.(jpg|jpeg|png|gif|css|js|svg)$", p.path, re.I)


def list_page_url(src: SourceConfig, page: int) -> str:
    if src.pagination == "trs":
        base = urllib.parse.urljoin(src.list_url, "./")
        return src.list_url if page == 1 else urllib.parse.urljoin(base, f"index_{page-1}.html")
    if "{page}" in src.list_url:
        return src.list_url.format(page=page, page0=page-1)
    if "{page0}" in src.list_url:
        return src.list_url.format(page0=page-1)
    return src.list_url


class Collector:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg.fetch.user_agent
        self._last_request = 0.0

    def close(self):
        self.session.close()

    def fetch(self, url: str, timeout: Optional[int] = None, params: Optional[dict] = None) -> FetchResult:
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            return FetchResult(ok=False, url=url, error="仅允许 HTTP/HTTPS 下载")
        last_err = ""
        for attempt in range(self.cfg.fetch.retries + 1):
            wait = self.cfg.fetch.request_interval_seconds - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                with self.session.get(url, params=params, timeout=timeout or self.cfg.fetch.timeout_seconds,
                                      verify=self.cfg.fetch.verify_ssl, stream=True) as resp:
                    # Authentication / blocking / missing pages require source maintenance, not aggressive retries.
                    if resp.status_code in (401, 403, 404):
                        return FetchResult(ok=False, url=url, error=f"HTTP {resp.status_code}")
                    resp.raise_for_status()
                    chunks, size = [], 0
                    for chunk in resp.iter_content(65536):
                        size += len(chunk)
                        if size > self.cfg.fetch.max_page_bytes:
                            return FetchResult(ok=False, url=url, error="超过单文件大小上限")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    return FetchResult(ok=True, url=url, final_url=resp.url, content=data,
                                       content_type=resp.headers.get("Content-Type", ""),
                                       sha256=hashlib.sha256(data).hexdigest())
            except requests.RequestException as e:
                last_err = type(e).__name__
                if attempt < self.cfg.fetch.retries:
                    time.sleep(min(10, self.cfg.fetch.retry_backoff_seconds * (attempt + 1)))
        return FetchResult(ok=False, url=url, error=last_err)

    def save(self, source_name: str, url: str, content: bytes, ext: str = "html") -> str:
        folder = self.cfg.downloads_dir / re.sub(r"[^\w.-]", "_", source_name)
        folder.mkdir(parents=True, exist_ok=True)
        suffix = re.sub(r"[^a-z0-9]", "", ext.lower())[:10] or "bin"
        path = folder / f"{hashlib.sha256(content).hexdigest()}.{suffix}"
        if not path.exists():
            path.write_bytes(content)
        # Absolute paths work with POLICY_DATA_DIR outside the checkout, too.
        return str(path.resolve())

    def is_policy_url(self, url: str, src: SourceConfig) -> bool:
        return allowed_url(url, src)


class ListPageParser:
    def __init__(self, src: SourceConfig):
        self.src = src

    def parse(self, html: str | bytes, base_url: str = "") -> list[CandidateLink]:
        soup = BeautifulSoup(html, "lxml")
        # Some public CMS list fragments are embedded as inert CDATA/comments
        # (e.g. Jiangsu jpage: HTML carries the first page as XML <record><![CDATA[…]]>).
        from bs4 import CData, Comment
        for block in soup.find_all(string=lambda x: isinstance(x, (Comment, CData))):
            if "<a " in str(block):
                block.replace_with(BeautifulSoup(str(block), "lxml"))
        selector = self.src.link_selector
        anchors = soup.select(selector) if selector else soup.find_all("a")
        # TRS jpage (Jiangsu/…): 首屏列表以 <script> 内嵌 <datastore><recordset> XML 序列化，
        # CDATA 记录内是 <li><a …>；剥掉 CDATA 标记后与页面 <a> 合并解析。
        for tag in soup.find_all("script"):
            txt = tag.string or ""
            if "<recordset>" not in txt or "<a " not in txt:
                continue
            frag = BeautifulSoup(re.sub(r"<!\[CDATA\[|\]\]>", "", txt), "lxml")
            anchors += frag.select(selector) if selector else frag.find_all("a")
        links, seen = [], set()
        for a in anchors:
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            href = normalized_url(urllib.parse.urljoin(base_url or self.src.list_url, href))
            if href in seen or not self._match(href):
                continue
            seen.add(href)
            title = a.get("title") or " ".join(a.get_text(" ", strip=True).split())
            links.append(CandidateLink(href, title[:300]))
        return links

    def _match(self, url: str) -> bool:
        return allowed_url(url, self.src)


def parse_gov_feed(raw: bytes, src: SourceConfig) -> list[CandidateLink]:
    """中国政府网公开栏目 JSON；字段与栏目网页自身脚本一致。"""
    import json
    rows = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(rows, list):
        raise ValueError('政府网公开列表结构变化：预期 JSON 数组')
    links, seen = [], set()
    for row in rows[:src.max_pages * 20]:
        if not isinstance(row, dict): continue
        url = normalized_url(urllib.parse.urljoin(src.list_url, str(row.get('URL',''))))
        if url not in seen and allowed_url(url, src):
            seen.add(url)
            links.append(CandidateLink(url, str(row.get('TITLE',''))))
    return links


# ---- 浙江系政府站"页面构建单元"动态列表（hanweb AuthorizedRead/unitbuild.js）----
# 栏目页不含列表，通过 <script ... unitbuild.js url="..." queryData="..."> 声明构建参数；
# 前端 GET url?queryData 得到 {"data":{"html":"<ul>…政策列表…</ul>"}}。不执行网页脚本。

def extract_unitbuild_spec(page_html: str) -> tuple[str, dict] | None:
    """从栏目页提取 (接口相对/绝对 URL, queryData 参数 dict)。未命中返回 None。"""
    candidates = []
    for tag in BeautifulSoup(page_html, "lxml").find_all("script"):
        if "AuthorizedRead/unitbuild.js" not in tag.get("src", ""):
            continue
        value = tag.get("querydata", "")
        try:
            try:
                params = json.loads(value)
            except json.JSONDecodeError:
                params = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            continue
        if isinstance(params, dict) and tag.get("url"):
            candidates.append((tag["url"], params))
    # 同页可能还有导航/页脚构建单元，优先信息列表。
    return next((x for x in candidates if x[1].get("tagId") == "信息列表"),
                candidates[0] if len(candidates) == 1 else None)


class DiscoveryError(ValueError):
    """列表未完整采完；此前成功页的候选仍可入队。"""

    def __init__(self, message: str, links: list[CandidateLink]):
        super().__init__(message)
        self.links = list(links)


def unit_list_links(list_html: str, src: SourceConfig, base_url: str = "") -> list[CandidateLink]:
    """从单元构建返回的 data.html 提取详情链接；标题优先取 <a title> 完整属性。"""
    soup = BeautifulSoup(list_html, "lxml")
    links, seen = [], set()
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        href = normalized_url(urllib.parse.urljoin(base_url or src.list_url, href))
        if href in seen or not allowed_url(href, src):
            continue
        seen.add(href)
        title = a.get("title") or a.get_text(" ", strip=True) or ""
        links.append(CandidateLink(href, " ".join(title.split())[:300]))
    return links


def discover_zj_unit_links(collector: Collector, src: SourceConfig, page_raw: bytes,
                           page_url: str, max_pages: int = 1, page_size: int = 15) -> list[CandidateLink]:
    """浙江系 unitbuild 两跳发现：栏目页 → 单元构建接口(data.html) → 详情链接。

    翻页：每页带 paramJson={"pageNo":N,"pageSize":15,"search":""}（与 unitbuild.js 的
    paramsMap 分支同构），空页自动停止；接口 JSON 原件逐页落盘留证。
    """
    spec = extract_unitbuild_spec(page_raw.decode("utf-8", "ignore"))
    if spec is None:
        raise ValueError("栏目页未发现 unitbuild 构建参数（结构变化或反爬页）")
    api_url, base_params = spec
    api_url = urllib.parse.urljoin(page_url, api_url)
    endpoint, origin = urllib.parse.urlsplit(api_url), urllib.parse.urlsplit(page_url)
    if (endpoint.scheme, endpoint.netloc) != (origin.scheme, origin.netloc) or not endpoint.path.startswith("/api-gateway/"):
        raise ValueError("列表构建接口不在官网同源公开网关内")
    links, seen = [], set()
    for page_no in range(1, max_pages + 1):
        params = dict(base_params)
        params["paramJson"] = json.dumps(
            {"pageNo": page_no, "pageSize": page_size, "search": ""},
            ensure_ascii=False)
        result = collector.fetch(api_url, params=params)
        if not result.ok:
            raise DiscoveryError(f"单元构建接口第{page_no}页请求失败: {result.error}", links)
        collector.save(src.name, api_url, result.content, "json")
        try:
            payload = json.loads(result.content.decode("utf-8", "ignore"))
        except json.JSONDecodeError as e:
            raise DiscoveryError(f"单元构建接口第{page_no}页返回非 JSON", links) from e
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise DiscoveryError(f"单元构建接口第{page_no}页返回失败或结构变化", links)
        data = payload.get("data") or {}
        list_html = data.get("html", "") if isinstance(data, dict) else ""
        if not isinstance(list_html, str) or not list_html:
            raise DiscoveryError("单元构建返回为空（列表未生成）", links)
        page_links = unit_list_links(list_html, src, base_url=page_url)
        fresh = [l for l in page_links if l.url not in seen]
        for l in fresh:
            seen.add(l.url)
        links.extend(fresh)
        if not page_links:      # 空页：翻页到底
            break
        if not fresh:           # 重复页不等于已采完，提示翻页参数/接口需维护。
            raise DiscoveryError(f"第{page_no}页完全重复，未确认历史列表采完", links)
    return links
