# -*- coding: utf-8 -*-
"""
解析 http://epub.cnipa.gov.cn/ 检索结果页 HTML，提取公布公告列表中的标题、公开号、详情链接、摘要（若有）。

与 `cnipa_epub_crawler.py` / **`cnipa_epub_search.py`**（一步检索+解析）配合：爬虫落盘 HTML 后可用本模块单独再解析；也可被其它脚本 import。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

EPUB_BASE = "http://epub.cnipa.gov.cn/"


@dataclass
class EpubSearchHit:
    """单条检索命中（字段随页面结构尽力解析，可能为空）。"""

    raw_html: str
    title: str | None = None
    pub_number: str | None = None
    link: str | None = None
    abstract: str | None = None
    applicant: str | None = None
    pub_date: str | None = None     # YYYY-MM-DD（申请公布日 / 授权公告日）


def _html_fragment_to_plain(html_snippet: str) -> str:
    """从一小段 HTML 抽取可读纯文本（用于摘要等）。"""
    t = re.sub(r"<script[^>]*>.*?</script>", "", html_snippet, flags=re.I | re.DOTALL)
    t = re.sub(r"<style[^>]*>.*?</style>", "", t, flags=re.I | re.DOTALL)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*全部\s*$", "", t).strip()
    return t


# 站点脚本把摘要 200 字以后折进隐藏的 span.alltxt，并在断点插一个「...」和一个「全部」链接。
# 不去掉这两样，摘要中间会凭空多出「 ... 」，还把断点处的词从中间劈开（「隔 ... 板四」）。
_FOLD_MARKERS = (
    re.compile(r"<i[^>]*class=[\"']point[\"'][^>]*>.*?</i>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<a[^>]*j-open-all(?:txt|info)[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL),
)


def _extract_abstract_from_item_html(item_html: str) -> str | None:
    """从单条 ``div.item`` 内 ``dt`` 摘要对应的 ``dd`` 中抽取全文（含折叠 span）。"""
    m = re.search(
        r'<dt[^>]*>\s*摘要\s*[：:]\s*</dt>\s*<dd[^>]*>(.*?)</dd>',
        item_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    frag = m.group(1)
    for marker in _FOLD_MARKERS:
        frag = marker.sub("", frag)
    # 折叠的 span 与可见部分本是一段连续文字：去标签时不能插空格
    frag = re.sub(r"</?span[^>]*>", "", frag, flags=re.IGNORECASE)
    plain = _html_fragment_to_plain(frag)
    return plain if len(plain) >= 4 else None


def _extract_dd(item_html: str, *labels: str) -> str | None:
    """取 ``<dt>标签：</dt><dd>值</dd>`` 的值（多个候选标签取第一个出现的）。"""
    m = re.search(
        r"<dt[^>]*>\s*(?:" + "|".join(map(re.escape, labels)) + r")\s*[：:]\s*</dt>\s*<dd[^>]*>(.*?)</dd>",
        item_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    frag = m.group(1)
    for marker in _FOLD_MARKERS:        # 多个申请人时后几个折在「全部」后面
        frag = marker.sub("", frag)
    plain = _html_fragment_to_plain(frag)
    return plain or None


def _normalize_date(raw: str | None) -> str | None:
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def _abs_url(href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return EPUB_BASE.rstrip("/") + href
    return EPUB_BASE.rstrip("/") + "/" + href.lstrip("/")


def parse_search_result_html(html: str, base_url: str = EPUB_BASE) -> list[EpubSearchHit]:
    """
    解析「公布公告」检索结果列表页 HTML。
    兼容常见表格行 / 带链接的条目（站点改版时需调整正则或选择器）。
    """
    _ = base_url  # 预留与绝对链接拼接策略扩展
    hits: list[EpubSearchHit] = []
    for m in re.finditer(
        r"<tr[^>]*>(.*?)</tr>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        row = m.group(1)
        low = row.lower()
        if "indexquery" in low or "searchstr" in low:
            continue
        title_m = re.search(
            r'title="([^"]+)"',
            row,
            re.IGNORECASE,
        ) or re.search(r">([^<]{6,200})<", row)
        title = title_m.group(1).strip() if title_m else None
        link_m = re.search(r'href="([^"]+)"', row)
        href = link_m.group(1).strip() if link_m else None
        link = _abs_url(href) if href else None
        pub_m = re.search(
            r"(CN\s*\d{9,}[A-Z]\s*|ZL\s*\d{9,}\.\d+)",
            row,
            re.IGNORECASE,
        )
        pub_number = pub_m.group(1).replace(" ", "") if pub_m else None
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)).strip()
        if len(text) < 8 and not pub_number:
            continue
        hits.append(
            EpubSearchHit(
                raw_html=row[:2000],
                title=title or (text[:200] if text else None),
                pub_number=pub_number,
                link=link,
            )
        )
    seen: set[str] = set()
    out: list[EpubSearchHit] = []
    for h in hits:
        key = h.pub_number or h.title or h.raw_html[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    if out:
        return out
    overview = _parse_overview_card_layout(html)
    if overview:
        return overview
    return _parse_search_result_fallback_links(html)


def _parse_overview_card_layout(html: str) -> list[EpubSearchHit]:
    """
    新版「公布模式」结果页：无表格行，每条为 ``div.item``，题名在 ``h1.title``，
    详情 URL 多在二维码 ``div.qrcode`` 的 ``title="http://epub.../patent/CN…"`` 上；
    摘要位于 ``dt`` 为「摘要」的 ``dd`` 内（含 ``span.alltxt`` 折叠段）。
    """
    low = html.lower()
    if "overview-default" not in low and 'class="item"' not in low:
        return []
    parts = re.split(r'(<div\s+class="item"\s*>)', html, flags=re.IGNORECASE)
    blocks: list[str] = []
    for j in range(1, len(parts) - 1, 2):
        blocks.append(parts[j] + parts[j + 1])
    if not blocks:
        return []

    base = EPUB_BASE.rstrip("/")
    hits: list[EpubSearchHit] = []
    for item_html in blocks:
        tm = re.search(
            r'<h1\s+class="title">\s*([^<]+?)\s*</h1>',
            item_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        title = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else None
        lm = re.search(
            r'title="(https?://epub\.cnipa\.gov\.cn/patent/[^"]+)"',
            item_html,
            flags=re.IGNORECASE,
        )
        link = lm.group(1).strip() if lm else None
        pm = re.search(
            r"(?:申请公布号|授权公告号)[：:]\s*</dt>\s*<dd>([^<]+?)</dd>",
            item_html,
            flags=re.IGNORECASE,
        )
        pub_number = None
        if pm:
            pub_number = pm.group(1).strip().replace(" ", "")
            if not re.match(r"^(?:CN|ZL)", pub_number, re.IGNORECASE):
                pub_number = None
        if not link and pub_number:
            link = f"{base}/patent/{pub_number}"
        if link:
            m_pub = re.search(
                r"/patent/((?:CN|ZL)[^/?#]+)",
                link,
                flags=re.IGNORECASE,
            )
            if m_pub and not pub_number:
                pub_number = m_pub.group(1).strip()
        abstract = _extract_abstract_from_item_html(item_html)
        applicant = _extract_dd(item_html, "申请人", "专利权人")
        pub_date = _normalize_date(_extract_dd(item_html, "申请公布日", "授权公告日", "公告日"))
        if not title and not pub_number and not link:
            continue
        raw = "|".join(
            x for x in (title, pub_number, link, (abstract or "")[:400]) if x
        )[:2000]
        hits.append(
            EpubSearchHit(
                raw_html=raw,
                title=title,
                pub_number=pub_number,
                link=link,
                abstract=abstract,
                applicant=applicant,
                pub_date=pub_date,
            )
        )
    seen: set[str] = set()
    out: list[EpubSearchHit] = []
    for h in hits:
        key = h.pub_number or h.link or (h.title or "")[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _parse_search_result_fallback_links(html: str) -> list[EpubSearchHit]:
    """从结果页中抽取指向公布详情的 <a href>。"""
    hits: list[EpubSearchHit] = []
    for m in re.finditer(
        r'<a\s+[^>]*href="([^"]+)"[^>]*>([^<]*)</a>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        href = (m.group(1) or "").strip()
        title = (m.group(2) or "").strip()
        if not href.startswith("/") and "epub.cnipa.gov.cn" not in href:
            continue
        hlow = href.lower()
        if not any(
            x in hlow
            for x in ("/dxb/", "/sw/", "/patent/", "detail", "show")
        ):
            continue
        if "indexForm" in href or "javascript:" in href.lower():
            continue
        low = href.lower()
        if "article" in low and "indexquery" in low:
            continue
        link = _abs_url(href)
        pub_m = re.search(r"(CN\s*\d{9,}[A-Z]?|ZL\s*\d{9,}\.\d+)", href + title, re.I)
        pub_number = pub_m.group(1).replace(" ", "") if pub_m else None
        raw = m.group(0)[:2000]
        if len(title) < 2 and not pub_number:
            continue
        hits.append(
            EpubSearchHit(
                raw_html=raw,
                title=title or None,
                pub_number=pub_number,
                link=link,
            )
        )
    seen: set[str] = set()
    out: list[EpubSearchHit] = []
    for h in hits:
        key = h.link or h.title or ""
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def hits_to_jsonable(hits: list[EpubSearchHit]) -> list[dict]:
    """供 JSON 序列化（不含 raw_html 过大字段时可裁剪）。"""
    rows = []
    for h in hits:
        d = asdict(h)
        d.pop("raw_html", None)
        rows.append(d)
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python cnipa_epub_parse.py <结果页.html>", file=sys.stderr)
        sys.exit(2)
    p = Path(sys.argv[1]).expanduser().resolve()
    html = p.read_text(encoding="utf-8")
    hits = parse_search_result_html(html)
    print(json.dumps(hits_to_jsonable(hits), ensure_ascii=False, indent=2))
