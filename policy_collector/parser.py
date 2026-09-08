"""文档解析模块：网页 HTML / PDF / Word 附件 → 结构化 Document。

边界说明：
- 扫描件 OCR 不在初版范围，解析失败时保留原文件并标记 parse_status=failed，
  进入补处理清单（与任务"附件解析失败仍保留原文件"口径一致）。
- 印章/签名等只能看页面图像，文本抽取不判定"有无印章/是否有效"。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from .models import Document

# 网页正文常见噪音块
_NOISE_SELECTORS = [
    "script", "style", "noscript", "header", "footer", "nav",
    ".toolbar", ".share", ".print", ".footer", ".header", ".breadcrumb",
]


class Parser:
    """按文件类型解析为 Document。"""

    # ---------- 元信息正则 ----------
    _WENHAO_RE = re.compile(r"([（(]?[A-Za-z\u4e00-\u9fa5]{2,20}?[）)]?[〔\[]\s*\d{4}\s*[\]〕]\s*\d{1,6}\s*号)")
    _DATE_RE = re.compile(r"(20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)")
    _AUTHOR_RE = re.compile(r"([\u4e00-\u9fa5]{2,30}(?:省|市|县|部|委|局|厅|办|中心|公司|集团))(?=\s*[（(]?文件|文号|〔|[\u4e00-\u9fa5]{0,3}印发)")

    def parse(self, data: bytes, fmt: str, page_url: str = "", origin_path: str = "") -> Document:
        fmt = fmt.lower().lstrip(".")
        if fmt in ("html", "htm", "shtml"):
            return self._html(data, page_url, origin_path)
        if fmt in ("pdf",):
            return self._pdf(data, page_url, origin_path)
        if fmt in ("docx", "doc"):
            return self._docx(data, page_url, origin_path)
        return Document(page_url=page_url, origin_path=origin_path, content="", doc_type="其他")

    # ---------- HTML ----------
    def _html(self, data: bytes, page_url: str, origin_path: str) -> Document:
        soup = BeautifulSoup(data, "lxml")
        # 站点详情适配优先（正文容器/标题/日期规则已收敛在 site_adapters）
        try:
            from .site_adapters import get_detail_adapter
            detail_fn = get_detail_adapter(page_url)
            if detail_fn is not None:
                parsed = detail_fn(soup, page_url)
                if parsed is not None and parsed.content:
                    doc = Document(
                        page_url=page_url,
                        origin_path=origin_path,
                        title=parsed.title or "",
                        content=parsed.content,
                        page_date=parsed.page_date,
                        attachments=parsed.attachments,
                    )
                    self._fill_meta(doc, doc.content[:800])
                    if not doc.title:
                        doc.title = Path(origin_path or page_url).stem[:200]
                    return doc
        except Exception:  # noqa: BLE001  适配器异常不阻断，回退通用解析
            pass

        for tag in soup.select(",".join(_NOISE_SELECTORS)):
            tag.decompose()
        title = (soup.title.get_text(strip=True) if soup.title else "") or ""
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)
        main = soup.find("div", class_=re.compile(r"(content|article|TRS_Editor)", re.I)) or soup.body or soup
        content = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
        text_head = content[:600]
        doc = Document(
            page_url=page_url,
            origin_path=origin_path,
            title=title[:300],
            content=content,
        )
        self._fill_meta(doc, text_head)
        return doc

    # ---------- PDF ----------
    def _pdf(self, data: bytes, page_url: str, origin_path: str) -> Document:
        try:
            from pypdf import PdfReader
            from io import BytesIO
            reader = PdfReader(BytesIO(data))
            pages = []
            for p in reader.pages[:60]:  # 防超大文件
                pages.append(p.extract_text() or "")
            content = re.sub(r"\n{3,}", "\n\n", "\n".join(pages)).strip()
        except Exception as e:  # noqa: BLE001
            return Document(page_url=page_url, origin_path=origin_path,
                            content="", doc_type="其他", issuing_authority=f"[PDF解析失败] {e}")
        doc = Document(page_url=page_url, origin_path=origin_path, content=content)
        self._fill_meta(doc, content[:600])
        if not doc.title:
            doc.title = Path(origin_path or page_url).stem[:200]
        return doc

    # ---------- Word ----------
    def _docx(self, data: bytes, page_url: str, origin_path: str) -> Document:
        try:
            from io import BytesIO
            import docx  # python-docx
            d = docx.Document(BytesIO(data))
            paras = [p.text for p in d.paragraphs if p.text.strip()]
            # 表格内容按行并入
            for t in d.tables[:10]:
                for row in t.rows:
                    paras.append(" | ".join(c.text.strip() for c in row.cells))
            content = re.sub(r"\n{3,}", "\n\n", "\n".join(paras)).strip()
        except Exception as e:  # noqa: BLE001
            return Document(page_url=page_url, origin_path=origin_path,
                            content="", doc_type="其他", issuing_authority=f"[Word解析失败] {e}")
        doc = Document(page_url=page_url, origin_path=origin_path, content=content)
        self._fill_meta(doc, content[:800])
        if not doc.title:
            doc.title = Path(origin_path or page_url).stem[:200]
        return doc

    # ---------- 元信息 ----------
    def _fill_meta(self, doc: Document, head: str) -> None:
        m = self._WENHAO_RE.search(head)
        if m:
            doc.wenhao = m.group(1).strip()
        dates = self._DATE_RE.findall(head)
        if dates:
            d = dates[0].replace("年", "-").replace("月", "-").replace("日", "").strip()
            parts = d.split("-")
            doc.doc_date = f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}" if len(parts) == 3 else d
        m2 = self._AUTHOR_RE.search(head)
        if m2:
            doc.issuing_authority = m2.group(1)
        if not doc.title and doc.wenhao:
            doc.title = doc.wenhao
