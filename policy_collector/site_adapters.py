"""站点适配器：列表页 + 详情页的站点差异统一收敛点。

两类差异分治：
1. 列表适配（发现候选详情链接）：默认「CSS 选择器 + include/exclude 过滤」即可；
   结构差异大的站点注册专属列表解析函数（见 register_list_parser）。
2. 详情适配（抽取正文/标题/日期/附件）：各官网正文容器、日期格式不一，
   按 URL 特征注册详情解析函数（见 register_detail_parser）。注册后 Parser
   解析 HTML 时自动优先调用，返回 None 则回退通用抽取。

适配状态（v0.1 实测）：
- ndrc.gov.cn 国家发展改革委：列表页静态直出 ul.u-list；详情页正文 div.TRS_Editor，
  通知正文完整（含文号/落款/成文日期），实施文件以附件 PDF 挂载 → 附件仅记录 URL，二期下载解析。
- fzggw.zj.gov.cn 浙江省发展改革委：新版栏目列表走省政务公开平台 xxgk 异步接口
  （需 token 的 JS 调用），初版不做列表适配，见 README「站点适配」的二期说明。
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Optional

from bs4 import BeautifulSoup

from .collector import CandidateLink
from .config import SourceConfig

# ---------------------------------------------------------------------------
# 列表页适配注册
# ---------------------------------------------------------------------------
_LIST_REGISTRY: dict[str, Callable] = {}


def register_list_parser(source_name: str):
    """为指定来源 name 注册专属列表解析函数。f(soup, src) -> list[CandidateLink]"""
    def deco(fn: Callable):
        _LIST_REGISTRY[source_name] = fn
        return fn
    return deco


def get_list_parser(source_name: str) -> Optional[Callable]:
    return _LIST_REGISTRY.get(source_name)


def has_custom_parser(source_name: str) -> bool:
    return source_name in _LIST_REGISTRY


# ---------------------------------------------------------------------------
# 详情页适配注册
# ---------------------------------------------------------------------------
@dataclass
class DetailResult:
    """站点详情页解析结果（None 表示该站无适配，回退通用解析）。"""

    title: str = ""
    content: str = ""
    page_date: str = ""                                    # YYYY-MM-DD（页面发布时间）
    attachments: list = field(default_factory=list)       # [{"name","url","fmt"}]


_DETAIL_REGISTRY: list[tuple[str, Callable]] = []  # (url特征, fn)，先注册先匹配


def register_detail_parser(url_mark: str):
    """按页面 URL 包含的片段注册详情解析器（如 'ndrc.gov.cn'）。"""
    def deco(fn: Callable):
        _DETAIL_REGISTRY.append((url_mark, fn))
        return fn
    return deco


def get_detail_adapter(page_url: str) -> Optional[Callable]:
    for mark, fn in _DETAIL_REGISTRY:
        if mark and mark in (page_url or ""):
            return fn
    return None


# ---------------------------------------------------------------------------
# NDRC 国家发展改革委详情页
# ---------------------------------------------------------------------------
_ATTACH_RE = re.compile(r"\.(pdf|doc|docx|wps|xls|xlsx|zip|rar|ofd)\s*$", re.I)


def _clean_text(s: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", (s or "").replace("\u3000", " ")).strip()


@register_detail_parser("ndrc.gov.cn")
def _ndrc_detail(soup: BeautifulSoup, page_url: str) -> Optional[DetailResult]:
    """正文容器 div.TRS_Editor（与 .article_con 同区）；标题取 <title>【…】内文。"""
    con = soup.select_one("div.TRS_Editor") or soup.select_one(".article_con")
    if con is None:
        return None
    content = _clean_text(con.get_text("\n", strip=True))
    if len(content) < 40:  # 正文过短视为无效页
        return None

    title = ""
    t = soup.title.get_text(strip=True) if soup.title else ""
    m = re.match(r"^【(.+?)】", t)
    title = m.group(1) if m else re.split(r"[_-]", t)[0].strip()

    page_date = ""
    m = re.search(r"发布时间[:：]\s*(20\d{2})/(\d{1,2})/(\d{1,2})",
                  soup.get_text(" ", strip=True))
    if m:
        page_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    attachments = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href.lower().startswith(("javascript", "mailto:")):
            continue
        if not _ATTACH_RE.search(href):
            continue
        abs_url = urllib.parse.urljoin(page_url, href)
        name = " ".join(a.get_text(strip=True).split()) or abs_url.rsplit("/", 1)[-1]
        attachments.append({
            "name": name[:200],
            "url": abs_url,
            "fmt": _ATTACH_RE.search(href).group(1).lower(),
        })

    return DetailResult(title=title[:300], content=content[:80000],
                        page_date=page_date, attachments=attachments)
